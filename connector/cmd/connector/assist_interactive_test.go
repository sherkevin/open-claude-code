package main

import (
	"bufio"
	"bytes"
	"context"
	"strings"
	"testing"
)

func TestAssistPromptRequiresCompleteExplicitInput(t *testing.T) {
	var out bytes.Buffer
	answer, err := assistPrompt(context.Background(), bufio.NewReader(strings.NewReader(" y \n")), &out, "confirm: ")
	if err != nil || answer != "y" || out.String() != "confirm: " {
		t.Fatalf("unexpected answer or prompt: %q %v %q", answer, err, out.String())
	}
	if _, err := assistPrompt(context.Background(), bufio.NewReader(strings.NewReader("y")), &out, "confirm: "); err == nil {
		t.Fatal("EOF without complete line must not authorize")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := assistPrompt(ctx, bufio.NewReader(strings.NewReader("y\n")), &out, "confirm: "); err == nil {
		t.Fatal("cancelled prompt must not authorize")
	}
}
