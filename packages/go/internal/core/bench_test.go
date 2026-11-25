package core

import (
	"strings"
	"testing"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// What building one call's event costs: a result of the size the limits keep, in ordinary text
// with a few things to redact.
func benchmarkCall(b *testing.B, text string) {
	block := jsonv.NewObject()
	block.Set("type", "text")
	block.Set("text", text)
	result := jsonv.NewObject()
	result.Set("content", []any{block})
	holder := jsonv.NewObject()
	holder.Set("value", result)
	arguments, _ := jsonv.Decode([]byte(`{"query":"orders of alice@example.com","limit":20,"filters":{"status":"open","since":"2026-01-01"}}`))
	input := jsonv.NewObject()
	input.Set("value", arguments)
	sdk := jsonv.NewObject()
	sdk.Set("name", "lsquare")
	ctx := &Context{SDK: sdk, Patterns: ResolvePatterns(nil), MaxEventBytes: DefaultMaxEventBytes}
	b.SetBytes(int64(len(text)))
	b.ReportAllocs()
	for b.Loop() {
		record := jsonv.NewObject()
		record.Set("callId", "c")
		record.Set("capturedAt", caseCapturedAt)
		record.Set("toolName", "search_orders")
		record.Set("status", "completed")
		record.Set("durationMs", jsonv.Int(12))
		record.Set("ids", jsonv.NewObject())
		record.Set("input", input)
		record.Set("result", holder)
		if _, failed := BuildCall(record, ctx, false); failed != nil {
			b.Fatal(failed)
		}
	}
}

func BenchmarkCallWithAShortResult(b *testing.B) {
	benchmarkCall(b, "3 orders found for the customer; the latest was placed on 2026-11-20 and shipped to Milan.")
}

func BenchmarkCallWithAFullResult(b *testing.B) {
	line := `{"id":"ord_1042","customer":"alice@example.com","total":129.5,"phone":"+1 415 555 2671","note":"call back after 5pm","ip":"192.168.1.20"}` + "\n"
	benchmarkCall(b, strings.Repeat(line, 4000/len(line)))
}

func BenchmarkCallWithALongResult(b *testing.B) {
	line := "The quarterly report shows revenue up in most regions, with returns handled by support@example.com within two days. "
	benchmarkCall(b, strings.Repeat(line, 200_000/len(line)))
}
