package main

import (
	"os"
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
	oldState, oldDefault := *modelState, *defaultModel
	t.Cleanup(func() { *modelState, *defaultModel = oldState, oldDefault })
	*modelState = state
	*defaultModel = "qwen-default"
	if got := selectedModel(); got != "qwen-default" {
		t.Fatalf("default model = %q", got)
	}
	if err := os.WriteFile(state, []byte("qwen3.8-27b\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := selectedModel(); got != "qwen3.8-27b" {
		t.Fatalf("selected model = %q", got)
	}
}

func TestStatusPriorityAndStates(t *testing.T) {
	resetLeases()
	t.Cleanup(resetLeases)
	oldState, oldDefault := *modelState, *defaultModel
	t.Cleanup(func() { *modelState, *defaultModel = oldState, oldDefault })
	*modelState = filepath.Join(t.TempDir(), "missing")
	*defaultModel = "qwen3.8-27b"

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
}
