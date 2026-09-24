package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
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
