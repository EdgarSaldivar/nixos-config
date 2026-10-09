package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func resetLeases() {
	mu.Lock()
	active = map[string]*lease{}
	mu.Unlock()
}

func TestSelectedModelUsesStateThenDefault(t *testing.T) {
	directory := t.TempDir()
	state := filepath.Join(directory, "profile")
	oldState, oldDefault, oldKnown := *modelState, *defaultModel, *knownProfiles
	t.Cleanup(func() { *modelState, *defaultModel, *knownProfiles = oldState, oldDefault, oldKnown })
	*modelState = state
	*defaultModel = "qwen-default"
	*knownProfiles = "qwen-default,qwen3.8-27b"
	if got := selectedModel(); got != "qwen-default" {
		t.Fatalf("default model = %q", got)
	}
	if err := os.WriteFile(state, []byte("qwen3.8-27b\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := selectedModel(); got != "qwen3.8-27b" {
		t.Fatalf("selected model = %q", got)
	}
	if err := os.WriteFile(state, []byte("removed-profile\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := selectedModel(); got != "qwen-default" {
		t.Fatalf("stale profile must report served default, got %q", got)
	}
}

func TestStatusPriorityAndStates(t *testing.T) {
	resetLeases()
	t.Cleanup(resetLeases)
	oldState, oldDefault, oldKnown := *modelState, *defaultModel, *knownProfiles
	t.Cleanup(func() { *modelState, *defaultModel, *knownProfiles = oldState, oldDefault, oldKnown })
	*modelState = filepath.Join(t.TempDir(), "missing")
	*defaultModel = "qwen3.8-27b"
	*knownProfiles = "qwen3.8-27b"

	state := statusSnapshot(func(string) string { return "active" }, func() bool { return true })
	if state["state"] != "gaming" {
		t.Fatalf("gaming must win, got %#v", state)
	}
	state = statusSnapshot(func(string) string { return "activating" }, func() bool { return true })
	if state["state"] != "gaming" {
		t.Fatalf("gaming activation must win, got %#v", state)
	}

	unit := func(name string) string {
		if name == *gamingUnit {
			return "inactive"
		}
		return "active"
	}
	state = statusSnapshot(unit, func() bool { return true })
	if state["state"] != "ready" {
		t.Fatalf("healthy idle endpoint = %#v", state)
	}

	mu.Lock()
	active["test"] = &lease{id: "test"}
	mu.Unlock()
	state = statusSnapshot(unit, func() bool { return true })
	if state["state"] != "busy" {
		t.Fatalf("active lease = %#v", state)
	}
	resetLeases()

	state = statusSnapshot(unit, func() bool { return false })
	if state["state"] != "loading" {
		t.Fatalf("active unready unit = %#v", state)
	}

	state = statusSnapshot(func(string) string { return "inactive" }, func() bool { return false })
	if state["state"] != "degraded" {
		t.Fatalf("inactive unready unit = %#v", state)
	}

	state = statusSnapshot(func(string) string { return "unknown" }, func() bool { return true })
	if state["state"] != "degraded" || state["detail"] != "gaming target state unknown" {
		t.Fatalf("unknown gaming state must fail closed = %#v", state)
	}
}

func TestHandlersFailClosedWhenGamingStateIsUnknown(t *testing.T) {
	resetLeases()
	t.Cleanup(resetLeases)
	acquireCalled := false
	released := false
	handler := newHandler(handlerDeps{
		unitState: func(string) string { return "unknown" },
		ready:     func() bool { return true },
		acquire: func(string) (*exec.Cmd, error) {
			acquireCalled = true
			return &exec.Cmd{}, nil
		},
		release: func(*lease) { released = true },
	})

	request := httptest.NewRequest(http.MethodPost, "/lease", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable || acquireCalled {
		t.Fatalf("unknown gaming acquire = %d, acquire=%v", response.Code, acquireCalled)
	}

	mu.Lock()
	active["held"] = &lease{id: "held"}
	mu.Unlock()
	request = httptest.NewRequest(http.MethodPost, "/lease/held/renew", nil)
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable || !released {
		t.Fatalf("unknown gaming renew = %d, released=%v", response.Code, released)
	}
	mu.Lock()
	_, stillHeld := active["held"]
	mu.Unlock()
	if stillHeld {
		t.Fatal("unsafe lease survived unknown gaming state")
	}
}

func TestStatusHandlerUsesInjectedHealthChecks(t *testing.T) {
	handler := newHandler(handlerDeps{
		unitState: func(string) string { return "unknown" },
		ready:     func() bool { return true },
		acquire:   acquire,
		release:   release,
	})
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/status", nil))
	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["state"] != "degraded" || payload["detail"] != "gaming target state unknown" {
		t.Fatalf("status response = %#v", payload)
	}
}

func TestEnrichReportsWhatIsServedAndRestartLimit(t *testing.T) {
	old := *servingState
	t.Cleanup(func() { *servingState = old })
	dir := t.TempDir()
	*servingState = filepath.Join(dir, "serving")

	// No file: nothing is serving, and no serving fields are invented.
	snap := enrich(map[string]any{"state": "degraded", "detail": "x"}, func(string) string { return "success" }, time.Unix(1000, 0), nil)
	if _, ok := snap["serving_profile"]; ok {
		t.Fatalf("serving_profile without a serving file: %#v", snap)
	}

	if err := os.WriteFile(*servingState, []byte("profile=glm-4.6v-flash\nengine=vllm\nstarted=900\nfallback=1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	snap = enrich(map[string]any{"state": "loading"}, func(string) string { return "success" }, time.Unix(1000, 0), nil)
	if snap["serving_profile"] != "glm-4.6v-flash" || snap["engine"] != "vllm" || snap["since_seconds"] != int64(100) || snap["fallback"] != true {
		t.Fatalf("serving fields = %#v", snap)
	}
	if _, ok := snap["restart_limited"]; ok {
		t.Fatalf("restart_limited without start-limit-hit: %#v", snap)
	}

	snap = enrich(map[string]any{"state": "degraded", "detail": "inference service unavailable"}, func(string) string { return "start-limit-hit" }, time.Unix(1000, 0), nil)
	if snap["restart_limited"] != true || snap["detail"] != "inference unit hit its restart limit" {
		t.Fatalf("restart limit not reported: %#v", snap)
	}
}

func TestTriedProfilesAreSelectableAndListed(t *testing.T) {
	dir := t.TempDir()
	state := filepath.Join(dir, "profile")
	users := filepath.Join(dir, "profiles.d")
	if err := os.MkdirAll(users, 0o755); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(users, "gemma-x.json"), []byte(`{"engine":"vllm","repo":"org/gemma-x","label":"gemma-x (tried)"}`), 0o644)
	os.WriteFile(filepath.Join(users, "Bad_Name.json"), []byte(`{}`), 0o644)

	oldState, oldUsers, oldKnown := *modelState, *userProfiles, *knownProfiles
	defer func() { *modelState, *userProfiles, *knownProfiles = oldState, oldUsers, oldKnown }()
	*modelState, *userProfiles, *knownProfiles = state, users, "qwen3.8-27b"

	os.WriteFile(state, []byte("gemma-x\n"), 0o644)
	if got := selectedModel(); got != "gemma-x" {
		t.Fatalf("tried profile not selectable: %q", got)
	}
	// A path-like or unknown name falls back to the default, never a file probe outside the dir.
	for _, bad := range []string{"../profile", "missing", "Bad_Name"} {
		os.WriteFile(state, []byte(bad), 0o644)
		if got := selectedModel(); got != *defaultModel {
			t.Fatalf("%q selected %q", bad, got)
		}
	}
	got := triedProfiles()
	if len(got) != 1 || got[0]["name"] != "gemma-x" || got[0]["engine"] != "vllm" {
		t.Fatalf("listed %v", got)
	}
}

func TestPalantirStateFollowsSwitchAndUnit(t *testing.T) {
	dir := t.TempDir()
	off := filepath.Join(dir, "palantir-off")
	oldUnit, oldOff := *palantirUnit, *palantirOff
	defer func() { *palantirUnit, *palantirOff = oldUnit, oldOff }()
	*palantirUnit, *palantirOff = "docker-palantir.service", off

	active := func(string) string { return "active" }
	inactive := func(string) string { return "inactive" }
	if got := enrich(map[string]any{}, nil, time.Unix(0, 0), active)["palantir"]; got != "running" {
		t.Fatalf("active unit: %v", got)
	}
	if got := enrich(map[string]any{}, nil, time.Unix(0, 0), inactive)["palantir"]; got != "waiting" {
		t.Fatalf("on but skipped: %v", got)
	}
	os.WriteFile(off, nil, 0o644)
	if got := enrich(map[string]any{}, nil, time.Unix(0, 0), active)["palantir"]; got != "off" {
		t.Fatalf("switched off: %v", got)
	}
	*palantirUnit = ""
	if _, ok := enrich(map[string]any{}, nil, time.Unix(0, 0), active)["palantir"]; ok {
		t.Fatal("reported without a unit configured")
	}
}
