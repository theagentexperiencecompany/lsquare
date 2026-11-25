package core

// difference is why a shared case's Go event isn't the expected one, and which of the checks it
// is let off, each by the exact problem the test reports.
type difference struct {
	why   string
	allow []string
}

// knownDifferences are the shared cases whose Go event isn't the expected one. Both come from
// what a Go string can hold: not half of a surrogate pair.
var knownDifferences = map[string]difference{
	"ids.json: a turnId is measured as JSON writes it: 50,000 lone surrogates are 300,000 bytes, escaped": {
		why: "Go reads a lone surrogate as U+FFFD, which is three bytes where JSON's escape for the surrogate is six. " +
			"The turnId is then 150,000 bytes, within the 256 KB a turnId may take, so it is kept and the event's ID is hashed from it, " +
			"where the expected event drops it as too_large and has a random ID.",
		allow: []string{
			"eventId isn't a random UUID",
			`event.eventId shows more than expected: "k/175934a56959e05fa1cd1bd88a782693060c2efa8f5e5bb105c5621cce271370" (expected "$uuid")`,
			"event.turnId isn't in the expected event",
		},
	},
	"redaction.json: secrets that differ only in a lone surrogate differ": {
		why: "Go reads both lone surrogates as U+FFFD, so the two secrets are one value and share a number. Each is still replaced whole.",
	},
}
