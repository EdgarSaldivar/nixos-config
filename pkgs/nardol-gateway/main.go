// nardol-gateway: make a sleeping GPU host reachable over HTTP.
//
// Home Assistant's llama.cpp integration points here instead of at nardol.
// Nardol suspends to S3 when idle, and an HTTP connect does not send a magic
// packet — so without this, the first voice command after a quiet period fails
// instead of waiting. This wakes the host, waits for the model server to be
// genuinely ready, and forwards the request that triggered the wake.
//
// Two rules drive the design:
//
//  1. READINESS IS /health RETURNING 200, NOT A SUCCESSFUL TCP CONNECT.
//     llama-server accepts connections long before weights are loaded. Dialing
//     successfully and forwarding would send a real request into a server that
//     answers 503, which reads to the caller as a model failure.
//
//  2. NEVER REPLAY A REQUEST THAT REACHED THE MODEL. A chat completion can
//     fire tool calls, and tool calls are side effects. Retrying one that may
//     already have turned on a light turns "nothing happened" into "it
//     happened twice", which is worse. Retry only connection attempts made
//     before any byte was written upstream.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

var (
	listen    = flag.String("listen", "127.0.0.1:8001", "address to serve on")
	upstream  = flag.String("upstream", "http://nardol:8000", "model server base URL")
	mac       = flag.String("mac", "", "MAC of the host to wake, aa:bb:cc:dd:ee:ff")
	broadcast = flag.String("broadcast", "10.0.0.255:9", "magic packet destination")
	wakeWait  = flag.Duration("wake-timeout", 90*time.Second, "how long to wait for readiness after waking")
	probeIvl  = flag.Duration("probe-interval", 1*time.Second, "readiness poll interval")
)

// One wake at a time. Ten simultaneous requests to a sleeping host must send
// one magic packet and then all wait on the same readiness result, not race.
var wake struct {
	sync.Mutex
	inFlight *sync.WaitGroup
	err      error
}

// modelsCache lets HA load its config entry without waking the GPU host. HA
// lists models when the integration starts; if that woke nardol, every restart
// of Home Assistant would spin up a 4090 to answer a question we already know
// the answer to.
var modelsCache struct {
	sync.RWMutex
	body []byte
}

func magicPacket(hw net.HardwareAddr) []byte {
	p := bytes.Repeat([]byte{0xff}, 6)
	for i := 0; i < 16; i++ {
		p = append(p, hw...)
	}
	return p
}

func sendWOL() error {
	if *mac == "" {
		return errors.New("no MAC configured")
	}
	hw, err := net.ParseMAC(*mac)
	if err != nil {
		return err
	}
	c, err := net.Dial("udp", *broadcast)
	if err != nil {
		return err
	}
	defer c.Close()
	_, err = c.Write(magicPacket(hw))
	return err
}

// ready reports whether the model server will actually serve a request.
func ready(ctx context.Context) bool {
	req, err := http.NewRequestWithContext(ctx, "GET", *upstream+"/health", nil)
	if err != nil {
		return false
	}
	resp, err := (&http.Client{Timeout: 3 * time.Second}).Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return resp.StatusCode == http.StatusOK
}

// ensureAwake blocks until the upstream is ready, waking it if needed.
// Concurrent callers share a single wake attempt.
func ensureAwake(ctx context.Context) error {
	if ready(ctx) {
		return nil
	}

	wake.Lock()
	if wake.inFlight != nil {
		wg := wake.inFlight
		wake.Unlock()
		wg.Wait()
		wake.Lock()
		err := wake.err
		wake.Unlock()
		if err != nil {
			return err
		}
		if ready(ctx) {
			return nil
		}
		return errors.New("upstream not ready after shared wake")
	}
	wg := &sync.WaitGroup{}
	wg.Add(1)
	wake.inFlight = wg
	wake.Unlock()

	err := func() error {
		log.Printf("upstream not ready; sending magic packet to %s via %s", *mac, *broadcast)
		if err := sendWOL(); err != nil {
			return err
		}
		deadline := time.Now().Add(*wakeWait)
		for time.Now().Before(deadline) {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(*probeIvl):
			}
			if ready(ctx) {
				log.Printf("upstream ready after %s", time.Since(deadline.Add(-*wakeWait)).Round(time.Second))
				return nil
			}
		}
		return errors.New("timed out waiting for upstream readiness")
	}()

	wake.Lock()
	wake.err = err
	wake.inFlight = nil
	wake.Unlock()
	wg.Done()
	return err
}

func serveModels(w http.ResponseWriter, r *http.Request) {
	// Serve from cache while asleep so HA setup never wakes the host.
	modelsCache.RLock()
	cached := modelsCache.body
	modelsCache.RUnlock()

	if ready(r.Context()) {
		if body, err := fetchUpstream(r.Context(), "/v1/models"); err == nil {
			modelsCache.Lock()
			modelsCache.body = body
			modelsCache.Unlock()
			w.Header().Set("Content-Type", "application/json")
			w.Write(body)
			return
		}
	}
	if cached != nil {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Nardol-Gateway", "cached")
		w.Write(cached)
		return
	}
	http.Error(w, `{"error":"upstream asleep and no cached model list"}`, http.StatusServiceUnavailable)
}

func fetchUpstream(ctx context.Context, path string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", *upstream+path, nil)
	if err != nil {
		return nil, err
	}
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("upstream status " + resp.Status)
	}
	return io.ReadAll(resp.Body)
}

func proxy(w http.ResponseWriter, r *http.Request) {
	// Buffer the body BEFORE waking. The request that triggered the wake is
	// the one the user is waiting on; it must survive the wait and be sent
	// exactly once afterwards.
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, `{"error":"could not read request"}`, http.StatusBadRequest)
		return
	}
	r.Body.Close()

	if err := ensureAwake(r.Context()); err != nil {
		log.Printf("wake failed for %s: %v", r.URL.Path, err)
		http.Error(w, `{"error":"inference host could not be woken"}`, http.StatusServiceUnavailable)
		return
	}

	out, err := http.NewRequestWithContext(r.Context(), r.Method, *upstream+r.URL.Path, bytes.NewReader(body))
	if err != nil {
		http.Error(w, `{"error":"bad upstream request"}`, http.StatusInternalServerError)
		return
	}
	for k, vs := range r.Header {
		if strings.EqualFold(k, "Host") || strings.EqualFold(k, "Connection") {
			continue
		}
		for _, v := range vs {
			out.Header.Add(k, v)
		}
	}

	// No timeout: a long generation is not a hung request, and cancellation
	// rides on the client's context instead.
	resp, err := (&http.Client{}).Do(out)
	if err != nil {
		// ⛔ DO NOT RETRY HERE. The request was written upstream; it may have
		// already executed tool calls. A replay could repeat a side effect.
		log.Printf("upstream error after send (not retrying): %v", err)
		http.Error(w, `{"error":"upstream failed mid-request"}`, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)

	// Stream, flushing as chunks arrive, so SSE reaches Home Assistant token
	// by token rather than in one lump at the end.
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 8192)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if rerr != nil {
			return
		}
	}
}

func main() {
	flag.Parse()
	if *mac == "" {
		log.Fatal("-mac is required")
	}

	mux := http.NewServeMux()
	// The gateway's own health must NOT depend on nardol: Home Assistant
	// treating the integration as broken because the GPU is asleep is exactly
	// the failure this service exists to prevent.
	mux.HandleFunc("/gateway/health", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"status": "ok", "upstream_ready": ready(r.Context())})
	})
	mux.HandleFunc("/v1/models", serveModels)
	mux.HandleFunc("/", proxy)

	log.Printf("nardol-gateway on %s -> %s (wake %s)", *listen, *upstream, *mac)
	srv := &http.Server{Addr: *listen, Handler: mux}
	if err := srv.ListenAndServe(); err != nil {
		log.Fatal(err)
		os.Exit(1)
	}
}
