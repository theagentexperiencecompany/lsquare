package core

// The ingestion's limits for one request, as the contract has them (packages/contract/src/limits.ts).
const (
	// MaxBatchEvents is the most events one request carries.
	MaxBatchEvents = 100
	// MaxBatchBytes is the largest request body.
	MaxBatchBytes = 1024 * 1024
	// MaxAckBytes is the largest acknowledgement the ingestion writes: it refuses a batch whose
	// acknowledgement could pass this.
	MaxAckBytes = 60 * 1024
)
