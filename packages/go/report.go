package lsquare

import (
	"bytes"
	"context"
	"os"
	"runtime"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

const (
	// Reports and log lines delivered per second, and held at once: so no loop of them, such as an
	// OnError that itself causes errors, can run away, and a handler that never returns can't hold
	// more.
	callbacksPerSecond = 100
	// Without OnError, problems are logged at most this often.
	errorLogInterval = time.Minute
	// How long Flush and Shutdown wait for a slow OnError or Logger before they return.
	deliveryWait = time.Second
)

// limiter is a rate, and at most callbacksPerSecond held at once, from admission until the handler
// returns, with event copies of at most maxBytes between them. One past the rate or the slots is
// dropped and counted; one past the bytes goes without its copy.
type limiter struct {
	tokens     float64
	last       time.Time
	held       int
	heldBytes  int
	suppressed int
}

func (l *limiter) take(bytes int, maxBytes int, now time.Time) (suppressed int, copied, ok bool) {
	if l.last.IsZero() {
		l.tokens = callbacksPerSecond
	} else if elapsed := now.Sub(l.last); elapsed > 0 {
		l.tokens = min(callbacksPerSecond, l.tokens+elapsed.Seconds()*callbacksPerSecond)
	}
	l.last = now
	if l.tokens < 1 || l.held >= callbacksPerSecond {
		l.suppressed++
		return 0, false, false
	}
	copied = bytes > 0 && (l.heldBytes == 0 || l.heldBytes+bytes <= maxBytes)
	l.tokens--
	l.held++
	if copied {
		l.heldBytes += bytes
	}
	suppressed = l.suppressed
	l.suppressed = 0
	return suppressed, copied, true
}

// delivery is one report or log line on its way to the app's handler.
type delivery struct {
	limit *limiter
	bytes int
	// A report.
	failure *Error
	event   []byte
	// A log line.
	level string
	line  string
}

// reporter delivers reports to OnError and lines to the Logger, one at a time, in order, from a
// goroutine of its own that runs only while there is something to deliver: never from the
// goroutine that recorded the event, and never with the client's lock held.
type reporter struct {
	logger   Logger
	onError  func(*Error, *Event)
	maxBytes int

	mu          sync.Mutex
	reportLimit limiter
	logLimit    limiter
	queue       []delivery
	running     bool
	queued      uint64
	delivered   uint64
	// Closed and replaced after each delivery.
	progress chan struct{}
	// Without OnError: when a problem was last logged, and how many weren't since.
	lastErrorLog     time.Time
	suppressedErrors int
	// The goroutine delivering, while a handler runs: a Flush called from the handler waits for
	// nothing, as it would be waiting for itself.
	delivering atomic.Uint64
}

type standardError struct{}

func (standardError) Info(message string) { _, _ = os.Stderr.WriteString(message + "\n") }
func (standardError) Warn(message string) { _, _ = os.Stderr.WriteString(message + "\n") }

func (r *reporter) init(logger Logger, onError func(*Error, *Event)) {
	r.logger = logger
	if r.logger == nil {
		r.logger = standardError{}
	}
	r.onError = onError
	r.progress = make(chan struct{})
}

// report queues a report. event is the event's JSON, when it was already redacted: only a custom
// handler sees it, so only then is it kept.
func (r *reporter) report(failure *Error, event []byte) {
	r.mu.Lock()
	defer r.mu.Unlock()
	size := 0
	if r.onError != nil {
		size = len(event)
	}
	suppressed, copied, ok := r.reportLimit.take(size, r.maxBytes, time.Now())
	if !ok {
		return
	}
	d := delivery{limit: &r.reportLimit, failure: failure}
	d.failure.Suppressed = suppressed
	if copied {
		d.event, d.bytes = event, size
	}
	r.enqueue(d)
}

// log queues a log line.
func (r *reporter) log(level, message string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	suppressed, _, ok := r.logLimit.take(0, 0, time.Now())
	if !ok {
		return
	}
	if suppressed > 0 {
		message += " [lsquare: " + strconv.Itoa(suppressed) + " earlier log lines suppressed]"
	}
	r.enqueue(delivery{limit: &r.logLimit, level: level, line: message})
}

// enqueue is called with the lock held.
func (r *reporter) enqueue(d delivery) {
	r.queue = append(r.queue, d)
	r.queued++
	if !r.running {
		r.running = true
		go r.deliver()
	}
}

func (r *reporter) deliver() {
	// A handler can end this goroutine (runtime.Goexit, as a test's FailNow does): another one
	// then takes over what is left.
	finished := false
	defer func() {
		if finished {
			return
		}
		r.mu.Lock()
		defer r.mu.Unlock()
		if len(r.queue) > 0 {
			go r.deliver()
		} else {
			r.running = false
		}
	}()
	for {
		r.mu.Lock()
		if len(r.queue) == 0 {
			r.running = false
			finished = true
			r.mu.Unlock()
			return
		}
		d := r.queue[0]
		r.queue[0] = delivery{}
		r.queue = r.queue[1:]
		r.mu.Unlock()
		r.counted(d)
	}
}

// counted runs one delivery and counts it as made, however its handler ends.
func (r *reporter) counted(d delivery) {
	defer func() {
		r.mu.Lock()
		d.limit.held--
		d.limit.heldBytes -= d.bytes
		r.delivered++
		close(r.progress)
		r.progress = make(chan struct{})
		r.mu.Unlock()
	}()
	r.run(d)
}

// run calls the app's handler. A handler that panics breaks nothing.
func (r *reporter) run(d delivery) {
	defer func() { _ = recover() }()
	id := goroutineID()
	r.delivering.Store(id)
	defer r.delivering.Store(0)
	handling.Store(id, struct{}{})
	handlers.Add(1)
	defer func() {
		handlers.Add(-1)
		handling.Delete(id)
	}()
	if d.failure == nil {
		r.write(d.level, d.line)
		return
	}
	if r.onError != nil {
		var event *Event
		if d.event != nil {
			if decoded, err := jsonv.Decode(d.event); err == nil {
				if object, ok := decoded.(*jsonv.Object); ok && object != nil {
					event = publicEvent(object, false)
				}
			}
		}
		r.onError(d.failure, event)
		return
	}
	// Reports the rate limit dropped before they got here count as suppressed too.
	r.suppressedErrors += d.failure.Suppressed
	now := time.Now()
	if !r.lastErrorLog.IsZero() && now.Sub(r.lastErrorLog) < errorLogInterval {
		r.suppressedErrors++
		return
	}
	more := ""
	if r.suppressedErrors > 0 {
		more = " (" + strconv.Itoa(r.suppressedErrors) + " more since the last report)"
	}
	r.lastErrorLog = now
	r.suppressedErrors = 0
	r.write("warn", "[lsquare] "+string(d.failure.Code)+": "+d.failure.Message+more)
}

func (r *reporter) write(level, line string) {
	if level == "info" {
		r.logger.Info(line)
	} else {
		r.logger.Warn(line)
	}
}

// deliveryGrace is how long Flush and Shutdown still wait for the app's handlers once their own
// deadline has passed: enough for a handler that only logs, so the last reports aren't lost with
// a process that exits next, and no more.
const deliveryGrace = 50 * time.Millisecond

// inHandler reports whether the caller is OnError or the Logger, being called now.
func (r *reporter) inHandler() bool {
	id := r.delivering.Load()
	return id != 0 && id == goroutineID()
}

// wait returns once the reports and lines queued so far have reached their handler. It waits
// until deadline or ctx's end, and then a moment more, and never longer than deliveryWait: a
// handler that is slow doesn't hold Flush past the time it was given. Called from a handler, it
// returns at once.
func (r *reporter) wait(ctx context.Context, deadline time.Time) {
	if r.inHandler() {
		return
	}
	now := time.Now()
	limit := now.Add(deliveryWait)
	if earliest := now.Add(deliveryGrace); deadline.Before(earliest) {
		limit = earliest
	} else if deadline.Before(limit) {
		limit = deadline
	}
	timer := time.NewTimer(time.Until(limit))
	defer timer.Stop()
	done := doneOf(ctx)
	r.mu.Lock()
	target := r.queued
	for r.delivered < target {
		progress := r.progress
		r.mu.Unlock()
		select {
		case <-progress:
		case <-timer.C:
			return
		case <-done:
			// Told to stop: the moment more, then no longer, and never past the limit.
			done = nil
			timer.Reset(max(min(deliveryGrace, time.Until(limit)), 0))
		}
		r.mu.Lock()
	}
	r.mu.Unlock()
}

// The goroutines delivering a report or a log line to the app's OnError or Logger now, of any
// client, and how many: a turn captured from one of those handlers is never recorded, as each
// would make another report or line, and their handler another turn.
var (
	handling sync.Map // goroutine ID -> struct{}
	handlers atomic.Int64
)

// inAnyHandler reports whether the caller is the OnError or the Logger of a client, being called
// now. It reads the goroutine's stack only while one is.
func inAnyHandler() bool {
	if handlers.Load() == 0 {
		return false
	}
	_, ok := handling.Load(goroutineID())
	return ok
}

var goroutinePrefix = []byte("goroutine ")

// goroutineID is the running goroutine's number, read from its stack's first line, as
// golang.org/x/net/http2 does for its own checks. Only used to tell whether Flush was called from
// OnError or the Logger.
func goroutineID() uint64 {
	var buffer [64]byte
	line := buffer[:runtime.Stack(buffer[:], false)]
	line = bytes.TrimPrefix(line, goroutinePrefix)
	if end := bytes.IndexByte(line, ' '); end > 0 {
		if id, err := strconv.ParseUint(string(line[:end]), 10, 64); err == nil {
			return id
		}
	}
	return 0
}
