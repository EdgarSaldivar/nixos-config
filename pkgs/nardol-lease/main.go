// nardol-lease: make "the host is awake" a fact you can hold, not observe.
//
// The wake gateway on pelargir can see nardol ready and still lose: the idle
// loop may call suspend a moment later, while the request is in flight. No
// amount of polling fixes that, because polling inverts systemd's contract.
// The contract is take-the-lock-THEN-work; an inhibitor taken after logind has
// admitted a sleep operation is too late and is refused outright.
//
// ⛔ THE ATOMICITY IS LOGIND'S, NOT THIS SERVICE'S. Both Inhibit() and the
// suspend request are handled by logind, so they serialise, and there are only
// two orders:
//
//  1. Inhibit() first -> we hold a block lock -> the later suspend request
//     sees it and does not proceed. Only then do we return 201, so the
//     gateway forwards knowing sleep is prohibited.
//  2. Suspend first -> logind marks the operation in progress -> our
//     Inhibit() FAILS. We return 409 and the gateway has not sent anything
//     upstream, so retrying the CONNECTION after resume is safe. Nothing was
//     replayed, because nothing was sent.
//
// A health check, a flag file or a /slots poll cannot provide that
// serialisation. This can, and it is the whole point of the service.
//
// Requires systemd >= 257, where `block` locks are enforced for privileged
// callers too (older semantics are now called `block-weak`). nardol runs 260.2.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

var (
	listen        = flag.String("listen", "0.0.0.0:8002", "address to serve on")
	ttl           = flag.Duration("ttl", 120*time.Second, "lease lifetime without renewal")
	healthURL     = flag.String("health-url", "http://127.0.0.1:8000/health", "inference health endpoint")
	modelState    = flag.String("model-state", "/var/lib/nardol-inference/profile", "selected model profile file")
	defaultModel  = flag.String("default-model", "qwen3.8-27b", "model profile when state is absent")
	knownProfiles = flag.String("known-profiles", "qwen3.8-27b", "comma-separated model profiles this generation can serve")
	gamingUnit    = flag.String("gaming-unit", "nardol-gaming.target", "systemd gaming unit")
	inferenceUnit = flag.String("inference-unit", "docker-ikllama.service", "systemd inference unit")
)

type lease struct {
	id      string
	cmd     *exec.Cmd // systemd-inhibit holding the block lock
	expires time.Time
}

var (
	mu     sync.Mutex
	active = map[string]*lease{}
	seq    int
)

// acquire takes a real logind block inhibitor. It returns an error if logind
// refuses — which is exactly what happens when a sleep operation is already in
// progress, and is the signal the gateway must not send its request.
func acquire(id string) (*exec.Cmd, error) {
	cmd := exec.Command("systemd-inhibit",
		"--what=sleep", "--mode=block",
		"--who=nardol-lease", "--why=inference request "+id,
		"sleep", "infinity")
	if err := cmd.Start(); err != nil {
		return nil, err
	}

	// systemd-inhibit exits immediately if Inhibit() was refused. Give it a
	// moment, then require BOTH that it is still running and that logind
	// actually lists the lock. Trusting "the process started" would accept a
	// lease that holds nothing.
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		return nil, fmt.Errorf("logind refused the inhibitor: %v", err)
	case <-time.After(400 * time.Millisecond):
	}

	out, err := exec.Command("systemd-inhibit", "--list", "--no-legend").Output()
	if err != nil || !strings.Contains(string(out), "nardol-lease") {
		cmd.Process.Kill()
		return nil, fmt.Errorf("inhibitor not visible to logind after acquire")
	}
	return cmd, nil
}

func release(l *lease) {
	if l.cmd != nil && l.cmd.Process != nil {
		l.cmd.Process.Kill()
	}
}

// reap drops leases whose holder stopped renewing. Without this a gateway
// crash, a killed request or a network partition would pin the host awake
// forever — the failure mode that makes people switch the whole thing off.
func reap() {
	for range time.Tick(5 * time.Second) {
		mu.Lock()
		for id, l := range active {
			if time.Now().After(l.expires) {
				log.Printf("lease %s expired; releasing", id)
				release(l)
				delete(active, id)
			}
		}
		mu.Unlock()
	}
}

func systemdState(unit string) string {
	out, err := exec.Command("systemctl", "show", "--property=ActiveState", "--value", unit).Output()
	if err != nil {
		return "unknown"
	}
	return strings.TrimSpace(string(out))
}

func selectedModel() string {
	value := *defaultModel
	if payload, err := os.ReadFile(*modelState); err == nil {
		if candidate := strings.TrimSpace(string(payload)); candidate != "" {
			for _, known := range strings.Split(*knownProfiles, ",") {
				if candidate == strings.TrimSpace(known) {
					value = candidate
					break
				}
			}
		}
	}
	return value
}

func inferenceReady() bool {
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get(*healthURL)
	if err != nil {
		return false
	}
	response.Body.Close()
	return response.StatusCode == http.StatusOK
}

func gamingStatus(unitState func(string) string) (inProgress bool, known bool) {
	state := unitState(*gamingUnit)
	switch state {
	case "active", "activating", "deactivating":
		return true, true
	case "inactive", "failed":
		return false, true
	default:
		return false, false
	}
}

func statusSnapshot(
	unitState func(string) string,
	readyCheck func() bool,
) map[string]any {
	model := selectedModel()
	game, known := gamingStatus(unitState)
	if !known {
		return map[string]any{"state": "degraded", "detail": "gaming target state unknown", "model_profile": model}
	}
	if game {
		return map[string]any{"state": "gaming", "detail": "gaming target active", "model_profile": model}
	}

	mu.Lock()
	leaseCount := len(active)
	mu.Unlock()
	inferenceState := unitState(*inferenceUnit)
	ready := readyCheck()
	switch {
	case leaseCount > 0:
		return map[string]any{"state": "busy", "detail": fmt.Sprintf("%d inference lease(s) active", leaseCount), "model_profile": model}
	case ready:
		return map[string]any{"state": "ready", "detail": "inference endpoint healthy", "model_profile": model}
	case inferenceState == "active" || inferenceState == "activating":
		return map[string]any{"state": "loading", "detail": "inference service loading", "model_profile": model}
	default:
		return map[string]any{"state": "degraded", "detail": "inference service unavailable", "model_profile": model}
	}
}

type handlerDeps struct {
	unitState func(string) string
	ready     func() bool
	acquire   func(string) (*exec.Cmd, error)
	release   func(*lease)
}

func newHandler(deps handlerDeps) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/lease", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		game, known := gamingStatus(deps.unitState)
		if !known {
			w.WriteHeader(http.StatusServiceUnavailable)
			json.NewEncoder(w).Encode(map[string]string{"error": "gaming state unknown"})
			return
		}
		if game {
			w.WriteHeader(http.StatusConflict)
			json.NewEncoder(w).Encode(map[string]string{"error": "gaming in progress"})
			return
		}
		mu.Lock()
		seq++
		id := fmt.Sprintf("l%d-%d", time.Now().Unix(), seq)
		mu.Unlock()

		cmd, err := deps.acquire(id)
		if err != nil {
			// ⛔ 409 MEANS "SLEEP ALREADY WON". The gateway must NOT forward;
			// it should wait for resume and retry the connection.
			log.Printf("acquire %s refused: %v", id, err)
			w.WriteHeader(http.StatusConflict)
			json.NewEncoder(w).Encode(map[string]string{"error": "suspend in progress"})
			return
		}
		// Close the check/acquire race in the conservative direction. The target
		// may have started while logind granted the unrelated sleep inhibitor.
		game, known = gamingStatus(deps.unitState)
		if !known || game {
			deps.release(&lease{cmd: cmd})
			if !known {
				w.WriteHeader(http.StatusServiceUnavailable)
				json.NewEncoder(w).Encode(map[string]string{"error": "gaming state unknown"})
				return
			}
			w.WriteHeader(http.StatusConflict)
			json.NewEncoder(w).Encode(map[string]string{"error": "gaming in progress"})
			return
		}
		mu.Lock()
		active[id] = &lease{id: id, cmd: cmd, expires: time.Now().Add(*ttl)}
		n := len(active)
		mu.Unlock()
		log.Printf("lease %s acquired (%d active)", id, n)
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{"id": id, "ttl_seconds": ttl.Seconds()})
	})

	mux.HandleFunc("/lease/", func(w http.ResponseWriter, r *http.Request) {
		renew := strings.HasSuffix(r.URL.Path, "/renew")
		if (renew && r.Method != http.MethodPost) || (!renew && r.Method != http.MethodDelete) {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		id := strings.TrimPrefix(r.URL.Path, "/lease/")
		id = strings.TrimSuffix(id, "/renew")
		game, known := gamingStatus(deps.unitState)
		if renew && (!known || game) {
			mu.Lock()
			if l, ok := active[id]; ok {
				deps.release(l)
				delete(active, id)
			}
			mu.Unlock()
			if !known {
				w.WriteHeader(http.StatusServiceUnavailable)
			} else {
				w.WriteHeader(http.StatusConflict)
			}
			return
		}
		mu.Lock()
		l, ok := active[id]
		if ok {
			switch {
			case r.Method == http.MethodDelete:
				deps.release(l)
				delete(active, id)
				log.Printf("lease %s released (%d active)", id, len(active))
			case renew:
				l.expires = time.Now().Add(*ttl)
			}
		}
		mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})

	mux.HandleFunc("/leases", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		ids := make([]string, 0, len(active))
		for id := range active {
			ids = append(ids, id)
		}
		mu.Unlock()
		json.NewEncoder(w).Encode(map[string]any{"active": ids})
	})

	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(statusSnapshot(deps.unitState, deps.ready))
	})
	return mux
}

func main() {
	flag.Parse()
	go reap()
	handler := newHandler(handlerDeps{
		unitState: systemdState,
		ready:     inferenceReady,
		acquire:   acquire,
		release:   release,
	})

	log.Printf("nardol-lease on %s (ttl %s)", *listen, *ttl)
	log.Fatal(http.ListenAndServe(*listen, handler))
}
