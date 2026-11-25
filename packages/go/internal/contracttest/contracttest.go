// Package contracttest is for this module's tests: it finds LSquare's contract (the shared cases
// and the ingestion's checks) and runs events through those checks.
//
// The contract is packages/contract of this repository, unless LSQUARE_CONTRACT_DIR says
// otherwise. It must be a checkout's, with its sources: the checks are run from src/validate.ts,
// which a released package doesn't carry. The tests that compare this SDK with the TypeScript one
// also read packages/lsquare/src, next to the contract. Nothing else outside packages/go is read.
package contracttest

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
)

// Dir is where the contract is.
func Dir(t testing.TB) string {
	t.Helper()
	dir := os.Getenv("LSQUARE_CONTRACT_DIR")
	if dir == "" {
		// packages/go/internal/contracttest/contracttest.go, four levels below packages.
		_, file, _, _ := runtime.Caller(0)
		dir = filepath.Join(filepath.Dir(file), "..", "..", "..", "contract")
	}
	abs, err := filepath.Abs(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(abs, "cases")); err != nil {
		t.Fatalf("the contract isn't at %s (set LSQUARE_CONTRACT_DIR): %v", abs, err)
	}
	return abs
}

// Missing ends a test that can't run for want of something outside packages/go (Node, the
// TypeScript SDK's sources): skipped, or failed when LSQUARE_REQUIRE_VALIDATOR is set, as it is
// in CI, where a check that silently didn't run would pass for one that did.
func Missing(t testing.TB, what string) {
	t.Helper()
	if os.Getenv("LSQUARE_REQUIRE_VALIDATOR") != "" {
		t.Fatal(what + " (and LSQUARE_REQUIRE_VALIDATOR is set)")
	}
	t.Skip(what)
}

// The ingestion's checks, given the JSON this SDK writes. One node process for all the events.
const script = `
import { readFileSync } from 'node:fs'
const { validateEvent } = await import(process.argv[1])
const lines = readFileSync(0, 'utf8').split('\n').filter(Boolean)
process.stdout.write(JSON.stringify(lines.map((line) => {
  const result = validateEvent(JSON.parse(line))
  return result.ok ? 'valid' : result.reason
})))
`

// Ingestion is what the ingestion says about each event, given as its JSON: valid, or its
// rejection reason. They are checked by the contract's own validator (src/validate.ts), through
// Node. Without Node the test is skipped, unless LSQUARE_REQUIRE_VALIDATOR is set, as it is in
// CI: there it fails.
func Ingestion(t testing.TB, events [][]byte) []string {
	t.Helper()
	contract := Dir(t)
	repo := filepath.Dir(filepath.Dir(contract))
	if _, err := exec.LookPath("node"); err != nil {
		Missing(t, "node isn't installed: the contract's validator (validate.ts) wasn't run on these events")
	}
	if _, err := os.Stat(filepath.Join(repo, "node_modules", "tsx", "package.json")); err != nil {
		t.Fatalf("tsx is missing from %s: run npm install at the repo root", repo)
	}
	if len(events) == 0 {
		return nil
	}
	validator := "file://" + filepath.ToSlash(filepath.Join(contract, "src", "validate.ts"))
	cmd := exec.Command("node", "--import", "tsx", "--input-type=module", "-e", script, validator)
	cmd.Dir = repo
	cmd.Stdin = bytes.NewReader(append(bytes.Join(events, []byte("\n")), '\n'))
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("the validator failed: %v\n%s", err, stderr.String())
	}
	var verdicts []string
	if err := json.Unmarshal(out, &verdicts); err != nil || len(verdicts) != len(events) {
		t.Fatalf("the validator answered %d verdicts for %d events: %v", len(verdicts), len(events), err)
	}
	return verdicts
}
