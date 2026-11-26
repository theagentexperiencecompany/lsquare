package core

import (
	"strings"
	"testing"
	"unsafe"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// An agent part shaped as it is captured makes the event the record's agent would, but where
// reading the call ran out of the scan budget: then it has its own (the differential test
// compares both with TypeScript's).
func TestAnAgentShapedAsCapturedMakesTheSameEvent(t *testing.T) {
	compared := 0
	for _, c := range loadCases(t) {
		if c.isTurn || c.call == nil {
			continue
		}
		if _, ok := asObject(field(c.call, "agent")); !ok {
			continue
		}
		want, wantFailed := buildCaseWith(t, c, false)
		if wantFailed == nil && strings.Contains(sentJSON(want.Event), CutMark) {
			continue
		}
		compared++
		got, gotFailed := buildCaseWith(t, c, true)
		switch {
		case (wantFailed == nil) != (gotFailed == nil):
			t.Errorf("%s: dropped as %+v, against %+v", c.id(), gotFailed, wantFailed)
		case wantFailed != nil:
			if wantFailed.Code != gotFailed.Code {
				t.Errorf("%s: dropped as %s, against %s", c.id(), gotFailed.Code, wantFailed.Code)
			}
		case sentJSON(got.Event) != sentJSON(want.Event):
			t.Errorf("%s: %s", c.id(), firstDifference(sentJSON(got.Event), sentJSON(want.Event)))
		}
	}
	if compared == 0 {
		t.Error("no shared case with an agent part")
	}
}

// Shaped as it is captured, the agent part has a budget of its own: an input that reads the
// call's whole budget leaves it whole, and what it read is charged in its place, so the result
// after it gets none.
func TestAnAgentShapedAsCapturedHasABudgetOfItsOwn(t *testing.T) {
	input := jsonv.NewObject()
	for i := range 60 {
		input.Set("k"+strings.Repeat("x", i), strings.Repeat("lorem ipsum ", 2_500))
	}
	holder := jsonv.NewObject()
	holder.Set("value", input)
	agent := jsonv.NewObject()
	agent.Set("answer", strings.Repeat("word ", 4_000))
	build := func(value any) *jsonv.Object {
		record := jsonv.NewObject()
		record.Set("callId", "call_1")
		record.Set("capturedAt", "2026-01-01T00:00:00.000Z")
		record.Set("toolName", "search_orders")
		record.Set("status", "completed")
		record.Set("durationMs", jsonv.Int(1))
		record.Set("ids", jsonv.NewObject())
		record.Set("input", holder)
		record.Set("agent", value)
		result := jsonv.NewObject()
		result.Set("value", "the result")
		record.Set("result", result)
		ctx := &Context{SDK: jsonv.NewObject(), MaxEventBytes: 10 << 20}
		draft, failed := BuildCall(record, ctx, false)
		if failed != nil {
			t.Fatalf("dropped as %+v", failed)
		}
		return draft.Event
	}
	answerOf := func(event *jsonv.Object) string {
		body, _ := asObject(field(event, "agent"))
		text, _ := field(body, "answer").(string)
		return text
	}
	if late := answerOf(build(agent)); late != CutMark {
		t.Errorf("shaped with the call, the answer is %d bytes", len(late))
	}
	early := build(ShapeAgent(agent, &Context{}))
	if got := answerOf(early); got != strings.Repeat("word ", 3_200)+"…[+4000 chars]" {
		t.Errorf("shaped as captured, the answer is %d bytes", len(got))
	}
	if strings.Contains(string(jsonv.Marshal(early)), "the result") {
		t.Errorf("the result after it was read: %s", jsonv.Marshal(field(early, "result")))
	}
}

// A shaped agent part holds strings of its own: a cut of the app's string doesn't keep the rest
// of it alive. What it holds is what it counts.
func TestAShapedAgentHoldsNothingOfTheApps(t *testing.T) {
	answer := strings.Repeat("word ", 200_000)
	input := jsonv.NewObject()
	input.Set("answer", answer)
	shaped := ShapeAgent(input, &Context{})
	cut, ok := field(shaped.body, "answer").(*cutString)
	if !ok {
		t.Fatalf("the answer was shaped as %T", field(shaped.body, "answer"))
	}
	from := uintptr(unsafe.Pointer(unsafe.StringData(answer)))
	at := uintptr(unsafe.Pointer(unsafe.StringData(cut.window)))
	if at >= from && at < from+uintptr(len(answer)) {
		t.Error("the cut is the app's string")
	}
	if size := shaped.Bytes(); size < len(cut.window) || size > len(cut.window)+1_000 {
		t.Errorf("it counts %d bytes for a cut of %d", size, len(cut.window))
	}
}
