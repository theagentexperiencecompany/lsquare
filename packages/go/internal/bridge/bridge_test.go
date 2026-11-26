package bridge

import (
	"testing"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

func agentOf(conversation string) *Agent {
	ids := jsonv.NewObject()
	ids.Set("conversationId", conversation)
	input := jsonv.NewObject()
	input.Set("answer", "It ships tomorrow.")
	return &Agent{IDs: ids, Shaped: core.ShapeAgent(input, &core.Context{})}
}

func TestTheFirstCaptureIsTheCallsAndTheCallKeepsNothingOnceItReturns(t *testing.T) {
	call := NewCall("call_1")
	if !call.Claim(func() *Agent { return agentOf("conv_1") }) {
		t.Fatal("the first capture wasn't the call's")
	}
	if call.Claim(func() *Agent { t.Error("a second capture was read as the call's"); return nil }) {
		t.Error("a second capture was the call's")
	}
	ended := call.Close()
	if ended.Agent == nil || ended.Agent.Bytes() <= 0 {
		t.Fatalf("the call ended without its agent part: %+v", ended)
	}
	// What a context the app keeps still holds: nothing of the turn.
	if call.agent != nil || call.turns != nil {
		t.Errorf("the call still holds %+v and %v", call.agent, call.turns)
	}
	if call.Claim(func() *Agent { return agentOf("conv_2") }) || call.Flush() {
		t.Error("a capture or a Flush after the return was the call's")
	}
}

// A capture from another goroutine, still being read as the tool returns, is too late: the call
// never waits for it, and it is a turn of its own.
func TestACaptureStillBeingReadAsTheToolReturnsIsATurnOfItsOwn(t *testing.T) {
	call := NewCall("call_1")
	reading, closed := make(chan struct{}), make(chan Ended)
	claimed := make(chan bool)
	go func() {
		claimed <- call.Claim(func() *Agent {
			close(reading)
			// Read on until the tool has returned.
			<-closed
			return agentOf("conv_1")
		})
	}()
	<-reading
	ended := call.Close()
	close(closed)
	if <-claimed {
		t.Error("a capture read past the tool's return was the call's")
	}
	if ended.Agent != nil || call.agent != nil {
		t.Errorf("the call has an agent part: %+v, %+v", ended.Agent, call.agent)
	}
}

// At the most turns a reply waits for, those already finished make room: the check runs with
// the call's lock held, and must take no other.
func TestFinishedTurnsMakeRoom(t *testing.T) {
	call := NewCall("call_1")
	finished := map[any]bool{}
	for i := range maxCallTurns {
		turn := new(int)
		*turn = i
		finished[turn] = i%2 == 0
		call.Follow(turn, func(turn any) bool { return finished[turn] })
	}
	call.Follow(new(int), func(turn any) bool { return finished[turn] })
	if got := len(call.Close().Turns); got != maxCallTurns/2+1 {
		t.Errorf("%d turns followed", got)
	}
}
