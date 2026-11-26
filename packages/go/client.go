package lsquare

import (
	"bytes"
	"container/list"
	"context"
	"io"
	"math"
	"math/rand/v2"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"runtime"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/bridge"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/core"
	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// The client's defaults, which are the TypeScript SDK's.
const (
	defaultFlushTimeout          = 2 * time.Second
	defaultShutdownTimeout       = 5 * time.Second
	defaultMaxConcurrentHooks    = 16
	defaultMaxConcurrentRequests = 4
	defaultMaxQueueEvents        = 10_000
	defaultMaxQueueBytes         = 10 * 1024 * 1024
	defaultMaxAttempts           = 3
	defaultRequestTimeout        = 10 * time.Second

	// The most of an acknowledgement that is read.
	maxResponseBytes = 64 * 1024
	// How many requests may be left behind, their transport ignoring its context, before no more
	// are started: Go can't stop them, so their number is bounded instead.
	maxAbandonedRequests = 16
	// While a daily limit holds, one batch is sent this often to check whether it was raised.
	quotaProbe    = 15 * time.Minute
	retryBase     = 250 * time.Millisecond
	retryCap      = 5 * time.Second
	retryAfterCap = 30 * time.Second
)

const (
	batchOverhead = len(`{"events":[]}`)
	ackOverhead   = len(`{"accepted":[],"duplicates":[],"rejected":[]}`)
	// As long as any resetsAt.
	longestResetsAt = "2000-01-01T00:00:00.000Z"
)

// Reasons the ingestion may give. Anything else is reported as "unspecified".
var rejectionReasons = map[string]bool{
	"missing_field": true, "invalid_field": true, "invalid_conversation_id": true, "too_large": true,
	"unsupported_schema_version": true, "quota_exceeded": true,
}

type mode int

const (
	modeDisabled mode = iota
	modeDryRun
	modeSend
)

type outcome int

const (
	outcomeNone outcome = iota
	outcomeSent
	outcomeSkipped
	outcomeFailed
)

type stage int

const (
	// Waiting for a hook slot.
	stageWaiting stage = iota
	// Inside BeforeSend.
	stageHook
	// BeforeSend has returned in time: what it returned is being shaped and redacted.
	stageReshape
	// Ready to send, waiting for a request slot.
	stageQueued
	stageSending
	// Waiting out a retry delay.
	stageRetry
)

// entry is one event, from when it is recorded until it is sent or dropped.
type entry struct {
	seq     uint64
	eventID string
	// The event's JSON: as BeforeSend gets it while it waits for the hook, final afterwards. Nil
	// once the entry is finished, so nothing still holding the entry holds content.
	json []byte
	// Until BeforeSend returns: what finishing the event needs.
	draft *core.Draft
	// What the entry holds, as the queue's limits count it.
	bytes int
	stage stage
	// Built-in redaction has run on the event.
	redacted  bool
	attempts  int
	retryAt   time.Time
	createdAt time.Time
	outcome   outcome
	// Its outcome is set: read without the lock.
	done atomic.Bool
	// Finished while its hook was still running: keeps its slot and bytes until the hook returns.
	zombie     bool
	batch      *batch
	timer      *time.Timer
	retryToken uint64
	// The retry it waits for, with the other events of its request.
	retry *retryGroup
	// A request for it was started: not merely given a slot.
	exchanged bool
	// Its hook has returned, set as it does and before the client's lock is taken: the hook's
	// deadline, coming due while its return waits for that lock, then leaves it alone.
	returned   atomic.Bool
	hookCancel context.CancelFunc
	live       *list.Element
	queued     *list.Element
	// For the dry run's line.
	ignoredFields         *jsonv.Object
	ignoredToolCallFields *jsonv.Object
	// How deep in the client's callbacks the event was captured: its BeforeSend runs one deeper.
	depth int
}

// batch is one request: the events it carries share its cancellation, which happens only once
// none of them is still live.
type batch struct {
	live   int
	cancel context.CancelFunc
	// Sent while a daily limit holds, to check whether it still does.
	probe    bool
	holdback *holdback
	entries  []*entry
}

// retryGroup is one request's events waiting to be sent again, together: one timer for them all,
// stopped once none of them waits for it any more.
type retryGroup struct {
	timer   *time.Timer
	waiting int
}

// holdback is holding back after quota_exceeded, until `until` or a check finds the limit raised.
// Events recorded meanwhile are dropped at once, before any hook, except one every 15 minutes:
// check, sent on its own to see whether the limit still holds.
type holdback struct {
	until     time.Time
	resetsAt  string
	nextCheck time.Time
	check     *entry
	// An event was told it is the check, and is still being built: no other one is meanwhile.
	checking bool
	dropped  int
}

// finishingCall is a call that has replied, whose event the wrapper is still building.
type finishingCall struct {
	callID string
	// What the wrapper holds of the call until its event is built, as the queue's limits count it.
	bytes int
	// Its bytes still count: until its event is queued or dropped.
	holding bool
	// While a daily limit holds, the call was told its event is the one that checks whether it
	// still does: the hold-back it checks, until its event is queued or the call gives up.
	probe    *holdback
	done     chan struct{}
	finished bool
	// Its event, once queued: kept here so a flush counts it even after it leaves the queue.
	entry *entry
}

// Client records tool calls and sends them to LSquare. Make one with New, for the whole process.
// Its methods are safe to call from any goroutine.
type Client struct {
	mode              mode
	apiKey            string
	url               string
	http              *http.Client
	beforeSend        func(context.Context, *Event) (*Event, error)
	flushTimeout      time.Duration
	beforeSendTimeout time.Duration
	requestTimeout    time.Duration
	maxHooks          int
	maxRequests       int
	maxQueueEvents    int
	maxQueueBytes     int
	maxEventBytes     int
	maxAttempts       int
	context           *core.Context
	reports           reporter

	mu             sync.Mutex
	live           *list.List // of *entry, oldest first
	liveBytes      int
	zombies        int
	zombieBytes    int
	waiting        *list.List // of *entry waiting for a hook slot
	queued         *list.List // of *entry ready to send
	activeHooks    int
	activeRequests int
	// Requests whose time ran out while their transport still hadn't returned.
	abandoned int
	// A pump is on its way: events that become ready before it runs go in one request.
	pumpScheduled bool
	identifying   int
	// The calls still being built whose bytes count, and what they hold together.
	finishingHeld  int
	finishingBytes int
	holdback       *holdback
	seq            uint64
	retryTokens    uint64
	closed         bool
	// Shutdown was called, and may still be waiting for the calls that had replied before it.
	closing        bool
	reportedClosed bool
	// The ID fields the last Identify that answered in time returned, on any server instrumented
	// with the client, and when it returned: nil before any. Under identifiedMu, which is taken
	// alone, so that noting an answer never waits on the client's other work.
	identifiedMu   sync.Mutex
	identifiedKeys []string
	identifiedAt   time.Time
	// What a tool call in a tool's context is known by, for this client: not the client itself,
	// which a context the app keeps would keep alive.
	owner     *int
	finishing map[string]*finishingCall
	// Closed and replaced whenever an event is finished or put off, to wake what waits on them.
	changed chan struct{}
	warned  map[string]bool
}

var warnedContactUserID sync.Once

func positive(value, fallback int) int {
	if value > 0 {
		return value
	}
	return fallback
}

func positiveDuration(value, fallback time.Duration) time.Duration {
	if value > 0 {
		return value
	}
	return fallback
}

// New makes a client. It never fails: without an API key, or with an endpoint it can't use
// (outside a dry run), the client is disabled, and says so once.
func New(options Options) *Client {
	c := &Client{
		live:      list.New(),
		waiting:   list.New(),
		queued:    list.New(),
		finishing: map[string]*finishingCall{},
		changed:   make(chan struct{}),
		warned:    map[string]bool{},
		owner:     new(int),
	}
	c.reports.init(options.Logger, options.OnError)
	c.flushTimeout = defaultFlushTimeout
	if options.FlushTimeout != 0 {
		c.flushTimeout = max(options.FlushTimeout, 0)
	}
	// When Flush doesn't wait, a hook still gets the default time.
	c.beforeSendTimeout = positiveDuration(options.BeforeSendTimeout, positiveDuration(c.flushTimeout, defaultFlushTimeout))
	c.maxHooks = positive(options.MaxConcurrentHooks, defaultMaxConcurrentHooks)
	c.maxRequests = positive(options.MaxConcurrentRequests, defaultMaxConcurrentRequests)
	c.maxQueueEvents = positive(options.MaxQueueEvents, defaultMaxQueueEvents)
	// Never so large that sizes added up could overflow.
	c.maxQueueBytes = min(positive(options.MaxQueueBytes, defaultMaxQueueBytes), math.MaxInt/4)
	// Never above the ingestion's own limit: a larger event would only be rejected there.
	c.maxEventBytes = min(positive(options.MaxEventBytes, core.DefaultMaxEventBytes), core.DefaultMaxEventBytes)
	c.maxAttempts = positive(options.MaxAttempts, defaultMaxAttempts)
	c.requestTimeout = positiveDuration(options.RequestTimeout, defaultRequestTimeout)
	c.beforeSend = options.BeforeSend
	c.reports.maxBytes = c.maxQueueBytes

	keys, skipped := core.ResolveKeys(options.RedactKeys)
	if skipped {
		c.log("warn", "[lsquare] Part of RedactKeys isn't a key name: it is ignored.")
	}
	sdk := jsonv.NewObject()
	sdk.Set("name", "lsquare")
	sdk.Set("version", Version)
	sdk.Set("language", "go")
	sdk.Set("runtime", runtimeName())
	c.context = &core.Context{SDK: sdk, Patterns: corePatterns(options.SkipRedaction), RedactKeys: keys, MaxEventBytes: c.maxEventBytes}

	// A private copy: redirects are never followed, so the key can't go elsewhere, whatever the
	// app's own client does with them, and it shares no cookies with the app's requests.
	client := http.Client{}
	if options.HTTPClient != nil {
		client = *options.HTTPClient
	}
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	client.Jar = nil
	c.http = &client

	dryRun := options.DryRun == DryRunOn
	if options.DryRun == DryRunFromEnv {
		env := os.Getenv("LSQUARE_DRY_RUN")
		dryRun = env == "1" || env == "true"
	}
	if dryRun {
		c.mode = modeDryRun
		return c
	}
	apiKey := strings.TrimSpace(options.APIKey)
	if apiKey == "" {
		c.log("warn", "[lsquare] No APIKey: capture is disabled.")
		return c
	}
	endpoint := options.Endpoint
	if endpoint == "" {
		endpoint = os.Getenv("LSQUARE_ENDPOINT")
	}
	if endpoint == "" {
		endpoint = DefaultEndpoint
	}
	target, problem := resolveEndpoint(endpoint)
	if problem != "" {
		c.log("warn", "[lsquare] "+problem+": capture is disabled.")
		return c
	}
	c.mode = modeSend
	c.url = target
	c.apiKey = apiKey
	return c
}

// runtimeName is the event's sdk.runtime: go and its major and minor version.
func runtimeName() string {
	version := strings.TrimPrefix(runtime.Version(), "go")
	if parts := strings.SplitN(version, ".", 3); len(parts) >= 2 {
		minor := parts[1]
		for i := 0; i < len(minor); i++ {
			if minor[i] < '0' || minor[i] > '9' {
				minor = minor[:i]
				break
			}
		}
		return "go/" + parts[0] + "." + minor
	}
	return "go"
}

// resolveEndpoint is the URL events are posted to, or what is wrong with the endpoint. The URL is
// written as a browser would resolve it: its scheme and host in lowercase, without the scheme's
// own port, its path without dot segments or slashes at its end.
func resolveEndpoint(raw string) (target, problem string) {
	const invalid = "The endpoint is not a valid URL"
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || parsed.Opaque != "" || parsed.Hostname() == "" {
		return "", invalid
	}
	if port := parsed.Port(); port != "" {
		if number, err := strconv.Atoi(port); err != nil || number < 0 || number > 65535 {
			return "", invalid
		}
	}
	scheme, host := strings.ToLower(parsed.Scheme), strings.ToLower(parsed.Hostname())
	authority := host
	if strings.HasPrefix(parsed.Host, "[") {
		// In brackets: an IPv6 address, without a zone, and nothing else.
		address, err := netip.ParseAddr(host)
		if err != nil || !address.Is6() || address.Zone() != "" {
			return "", invalid
		}
		host = address.String()
		authority = "[" + host + "]"
	} else if strings.Contains(host, ":") {
		// An IPv6 address without its brackets: where it ends and a port begins can't be told.
		return "", invalid
	}
	local := host == "localhost" || host == "127.0.0.1" || host == "::1"
	if scheme != "https" && !(scheme == "http" && local) {
		return "", "The endpoint must use https (http only for localhost)"
	}
	if port := parsed.Port(); port != "" {
		if number, _ := strconv.Atoi(port); !(scheme == "https" && number == 443) && !(scheme == "http" && number == 80) {
			authority += ":" + strconv.Itoa(number)
		}
	}
	target = scheme + "://" + authority + withoutDotSegments(parsed.EscapedPath()) + "/v1/turns"
	// What a request can't be made for is no endpoint.
	if _, err := http.NewRequest(http.MethodPost, target, nil); err != nil {
		return "", invalid
	}
	return target, ""
}

// withoutDotSegments is a URL's path as a browser resolves it: "." and ".." segments applied,
// written plainly or with %2e, an empty segment kept, and no slash left at its end.
func withoutDotSegments(escaped string) string {
	var kept []string
	for _, segment := range strings.Split(strings.TrimPrefix(escaped, "/"), "/") {
		switch strings.ToLower(segment) {
		case ".", "%2e":
		case "..", ".%2e", "%2e.", "%2e%2e":
			if len(kept) > 0 {
				kept = kept[:len(kept)-1]
			}
		default:
			kept = append(kept, segment)
		}
	}
	return strings.TrimRight("/"+strings.Join(kept, "/"), "/")
}

// broadcast wakes whatever waits for an event to be finished or put off. Called with the lock held.
func (c *Client) broadcast() {
	close(c.changed)
	c.changed = make(chan struct{})
}

func (c *Client) log(level, message string) { c.reports.log(level, message) }

func (c *Client) report(err *Error, event []byte) { c.reports.report(err, event) }

// guard runs a step of the client's own, so that a bug in it drops an event instead of crashing
// the app.
func (c *Client) guard(step func()) {
	defer func() {
		if recover() != nil {
			c.report(&Error{Code: CodeInternal, Message: "The SDK failed unexpectedly; an event may have been dropped."}, nil)
		}
	}()
	step()
}

// The wrapper's view of the client.

type bridged struct{ c *Client }

func init() {
	bridge.Of = func(client any) bridge.Client {
		c, ok := client.(*Client)
		if !ok || c == nil {
			return nil
		}
		return bridged{c}
	}
}

func (b bridged) Enabled() bool { return b.c.mode != modeDisabled }

func (b bridged) Closed() bool {
	b.c.mu.Lock()
	defer b.c.mu.Unlock()
	if b.c.closed || b.c.closing {
		// Asked as a call arrives: it will run unrecorded, which is said once.
		b.c.refuseClosed()
		return true
	}
	return false
}

func (b bridged) Ended() bool { return b.c.ending() }

// ending reports whether Shutdown has begun.
func (c *Client) ending() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.closed || c.closing
}

// refuseEnding reports whether Shutdown has begun, and if it has, says once that what comes now
// is dropped.
func (c *Client) refuseEnding() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed || c.closing {
		c.refuseClosed()
		return true
	}
	return false
}

func (b bridged) Record(record *jsonv.Object, diagnostic *bridge.Report) {
	b.c.guard(func() { b.c.enqueue(record, diagnostic) })
}

func (b bridged) Report(report bridge.Report) {
	b.c.report(&Error{Code: ErrorCode(report.Code), Message: report.Message}, nil)
}

func (b bridged) WarnOnce(key, message string) {
	b.c.mu.Lock()
	seen := b.c.warned[key]
	b.c.warned[key] = true
	b.c.mu.Unlock()
	if !seen {
		b.c.log("warn", message)
	}
}

func (b bridged) CallbackContext(ctx context.Context) context.Context {
	return bridge.MarkCallback(ctx, b.c)
}

func (b bridged) WithCall(ctx context.Context, call *bridge.Call) context.Context {
	return bridge.WithCall(ctx, b.c.owner, call)
}

func (b bridged) Identified(keys []string, at time.Time) {
	b.c.identifiedMu.Lock()
	defer b.c.identifiedMu.Unlock()
	if at.Before(b.c.identifiedAt) {
		return
	}
	b.c.identifiedKeys, b.c.identifiedAt = slices.Clone(keys), at
	if b.c.identifiedKeys == nil {
		b.c.identifiedKeys = []string{}
	}
}

func (b bridged) IdentifiedKeys() []string {
	b.c.identifiedMu.Lock()
	defer b.c.identifiedMu.Unlock()
	return b.c.identifiedKeys
}

func (b bridged) FlushTimeout() time.Duration { return b.c.flushTimeout }

// Settle waits for the call's event, built or still being built, and the turns given, as their
// own: other events go as they would, and no report or line is waited for. It does nothing more
// once its time is up.
func (b bridged) Settle(ctx context.Context, tracked bridge.Tracked, turns []any, from time.Time) {
	c := b.c
	deadline := from.Add(c.flushTimeout)
	if c.flushTimeout <= 0 || !time.Now().Before(deadline) {
		return
	}
	done := doneOf(ctx)
	over := func() bool { return isDone(done) || !time.Now().Before(deadline) }
	if over() {
		return
	}
	call, _ := tracked.(*finishingCall)
	c.mu.Lock()
	defer c.mu.Unlock()
	if call != nil && !call.finished {
		c.finishCalls(done, []*finishingCall{call}, deadline)
	}
	if over() {
		return
	}
	var targets []*entry
	if call != nil && call.entry != nil && call.entry.outcome == outcomeNone {
		targets = append(targets, call.entry)
	}
	for _, turn := range turns {
		if e, ok := turn.(*entry); ok && e.outcome == outcomeNone {
			targets = append(targets, e)
		}
	}
	if len(targets) == 0 {
		return
	}
	c.pumpHooks()
	c.pumpRequests()
	c.waitFor(done, targets, deadline)
}

// finishedEntry reports whether a queued event is sent or dropped, without the client's lock: a
// call's lock may be held.
func (c *Client) finishedEntry(event any) bool {
	e, ok := event.(*entry)
	if !ok {
		return true
	}
	return e.done.Load()
}

func (b bridged) AcquireHook() (func(), bool) {
	c := b.c
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.identifying >= c.maxHooks {
		return nil, false
	}
	c.identifying++
	var once sync.Once
	return func() {
		once.Do(func() {
			c.mu.Lock()
			c.identifying--
			c.mu.Unlock()
		})
	}, true
}

func (b bridged) Track(callID string, bytes int) (func(), bridge.Tracked, bool) {
	c := b.c
	call := &finishingCall{callID: callID, bytes: max(bytes, 0), holding: true, done: make(chan struct{})}
	c.mu.Lock()
	// Once Shutdown is called its event would be refused: nothing is dropped to make room for it.
	if c.closed || c.closing {
		c.refuseClosed()
		c.mu.Unlock()
		return nil, nil, false
	}
	// While a daily limit holds, whether the call's event would be dropped for it is decided now,
	// before anything of the call is kept: dropped, it is counted and nothing more is done for
	// it. A limit whose time is up ends here, and the call goes on as any other.
	if h := c.holdback; h != nil {
		decision := "drop"
		c.guardLocked(func() { decision = c.admit(h) })
		switch decision {
		case "drop":
			c.mu.Unlock()
			return nil, nil, false
		case "check":
			call.probe = h
		}
	}
	// Within the queue's limits, with the events it holds: room is made as for a new event, and
	// a call that can't fit even so (alone, or with what can't be dropped) is refused. The call
	// that checks a daily limit drops nothing to make room: without room it gives its turn up,
	// and is counted as dropped for the limit.
	room := false
	if h := call.probe; h != nil {
		if !c.fits(call.bytes) {
			h.checking = false
			h.dropped++
			c.mu.Unlock()
			return nil, nil, false
		}
		room = true
	} else {
		c.guardLocked(func() { room = c.makeRoom(call.bytes) })
	}
	if !room {
		c.mu.Unlock()
		c.report(&Error{Code: CodeQueueFull, Message: "The queue is full; the event was dropped.", EventID: callID}, nil)
		return nil, nil, false
	}
	c.finishing[callID] = call
	c.finishingHeld++
	c.finishingBytes += call.bytes
	c.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			c.mu.Lock()
			call.finished = true
			c.releaseCall(call)
			if call.probe != nil {
				// Its event was never recorded: the next one checks the limit instead.
				call.probe.checking = false
				call.probe = nil
			}
			if c.finishing[callID] == call {
				delete(c.finishing, callID)
			}
			c.mu.Unlock()
			close(call.done)
		})
	}, call, true
}

// releaseCall stops counting what a call being built holds: its event is queued, or dropped.
// Called with the lock held.
func (c *Client) releaseCall(call *finishingCall) {
	if call.holding {
		call.holding = false
		c.finishingHeld--
		c.finishingBytes -= call.bytes
	}
}

// Recording.

// refuseClosed reports, once, that an event came after Shutdown. Called with the lock held.
func (c *Client) refuseClosed() {
	if c.reportedClosed {
		return
	}
	c.reportedClosed = true
	c.report(&Error{Code: CodeShutdown, Message: "An event was recorded after Shutdown; this and later events are dropped."}, nil)
}

// enqueue builds a call's event and queues it. diagnostic (the call's identify_failed) is reported
// once, before what else is reported about the event, unless the event is refused for Shutdown,
// which is reported instead.
func (c *Client) enqueue(record *jsonv.Object, diagnostic *bridge.Report) {
	_ = c.enqueueEvent(kindCall, func() *jsonv.Object { return record }, "", diagnostic, 0)
}

// enqueueTurn builds a turn's event and queues it: input reads the turn, once the client takes
// it. depth is how deep in the client's callbacks it was captured: its BeforeSend runs one deeper.
// It returns the event as queued: nil when it isn't.
func (c *Client) enqueueTurn(input func() *jsonv.Object, capturedAt string, depth int) *entry {
	return c.enqueueEvent(kindTurn, input, capturedAt, nil, depth)
}

// The two kinds of event.
const (
	kindCall = "tool_call"
	kindTurn = "turn"
)

func (c *Client) enqueueEvent(kind string, read func() *jsonv.Object, capturedAt string, diagnostic *bridge.Report, depth int) (taken *entry) {
	if c.mode == modeDisabled {
		return
	}
	// A turn isn't taken once Shutdown has begun: what it waits for is the calls that had already
	// replied, whose events are still being built.
	if kind == kindTurn && c.refuseEnding() {
		return
	}
	var record *jsonv.Object
	say := func() {
		if diagnostic != nil {
			c.report(&Error{Code: ErrorCode(diagnostic.Code), Message: diagnostic.Message}, nil)
			diagnostic = nil
		}
	}
	// A call that replied may hold what its event needs: a turn, linked to a call or not, never does.
	callID := ""
	if kind == kindCall {
		record = read()
		callID, _ = fieldText(record, "callId")
	}
	admitted, said, probe := c.admitNew(callID)
	if !admitted {
		if said {
			say()
		}
		return
	}
	// The event told it checks a daily limit gives that up if it is never queued: the next one
	// then checks, where waiting out the interval would put recovery off by 15 minutes.
	queued := false
	defer func() {
		if probe != nil && !queued {
			c.mu.Lock()
			probe.checking = false
			c.mu.Unlock()
		}
	}()

	// Read only now: a turn the client doesn't take is never read.
	if record == nil {
		record = read()
	}
	// Built outside the lock: it reads and redacts the whole call.
	hook := c.beforeSend != nil
	var draft *core.Draft
	var failed *core.Failure
	built := func() (ok bool) {
		defer func() {
			if recover() != nil {
				ok = false
			}
		}()
		if kind == kindTurn {
			draft, failed = core.BuildTurn(record, capturedAt, c.context, hook)
		} else {
			draft, failed = core.BuildCall(record, c.context, hook)
		}
		return true
	}()
	if !built {
		say()
		c.report(&Error{Code: CodeRedactionFailed, Message: "Building the event failed; it was dropped, never sent unredacted."}, nil)
		return
	}
	if failed != nil {
		say()
		messages := map[string]string{
			"invalid_input": "The event isn't complete; it was dropped.",
			"invalid_id":    "An ID must be a non-empty string of at most 512 bytes, without NUL; the event was dropped.",
			"too_large":     "The event is larger than " + strconv.Itoa(c.maxEventBytes) + " bytes even without its largest parts; it was dropped.",
		}
		c.report(&Error{Code: ErrorCode(failed.Code), Message: messages[failed.Code], Paths: failed.Paths}, nil)
		return
	}
	event := draft.Event
	if userID, _ := fieldText(event, "userId"); userID != "" && core.LooksLikeContact(userID) {
		warnedContactUserID.Do(func() {
			c.log("warn", "[lsquare] A userId looks like an email address or phone number. It is sent as given, so the person can be deleted by it: send an internal ID instead.")
		})
	}
	encoded := jsonv.Marshal(event)
	// Waiting for its hook, it also holds what numbering needs across the hook.
	size := len(encoded)
	if hook {
		size += draft.RetainedBytes()
	}
	eventID, _ := fieldText(event, "eventId")
	// The event is kept as its JSON from here: the draft has no more use for it.
	draft.Event = nil

	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed || (kind == kindTurn && c.closing) {
		c.refuseClosed()
		return
	}
	// What the wrapper held of the call is the event from here: its room is the event's.
	var call *finishingCall
	if kind == kindCall {
		call = c.finishing[eventID]
	}
	if call != nil {
		c.releaseCall(call)
	}
	if !c.makeRoom(size) {
		say()
		c.report(&Error{Code: CodeQueueFull, Message: "The queue is full; the event was dropped.", EventID: eventID}, nil)
		return
	}
	// Admitted: from here what the call says of its identify is true, said before the event's own reports.
	say()
	c.seq++
	e := &entry{
		seq: c.seq, eventID: eventID, json: encoded, bytes: size, createdAt: time.Now(), redacted: !hook,
		ignoredFields: draft.IgnoredFields, ignoredToolCallFields: draft.IgnoredToolCallFields(), depth: depth,
	}
	if hook {
		e.draft = draft
	}
	e.live = c.live.PushBack(e)
	c.liveBytes += size
	taken = e
	if call != nil {
		call.entry = e
	}
	if probe != nil {
		queued = true
		probe.checking = false
		if c.holdback == probe && probe.check == nil {
			probe.check = e
			probe.nextCheck = time.Now().Add(quotaProbe)
		}
	}
	if hook {
		e.stage = stageWaiting
		e.queued = c.waiting.PushBack(e)
		e.timer = time.AfterFunc(c.beforeSendTimeout, func() { c.hookDeadline(e) })
		c.pumpHooks()
	} else {
		c.ready(e)
	}
	return taken
}

// admitNew says whether a new event is taken at all, before it is built. Not after Shutdown: the
// refusal is then reported, instead of what the call says of its identify. Not while a daily
// limit holds (said: the call's own report is still made), unless it is the one that checks
// whether the limit still holds: probe is then the hold-back it checks.
func (c *Client) admitNew(callID string) (admitted, said bool, probe *holdback) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		c.refuseClosed()
		return false, false, nil
	}
	// Decided already, as the call replied: its event is the check, if that limit still holds.
	if call := c.finishing[callID]; call != nil && call.probe != nil {
		probe, call.probe = call.probe, nil
		if c.holdback == probe {
			return true, false, probe
		}
		probe.checking = false
	}
	h := c.holdback
	if h == nil {
		return true, false, nil
	}
	switch c.admit(h) {
	case "drop":
		return false, true, nil
	case "check":
		return true, false, h
	}
	return true, false, nil
}

func fieldText(object *jsonv.Object, key string) (string, bool) {
	value, _ := object.Get(key)
	text, ok := value.(string)
	return text, ok
}

// Hooks.

// pumpHooks starts hooks for waiting events, within the concurrency limit. Called with the lock held.
func (c *Client) pumpHooks() {
	for c.activeHooks < c.maxHooks && c.waiting.Len() > 0 {
		e := c.waiting.Remove(c.waiting.Front()).(*entry)
		e.queued = nil
		if e.outcome != outcomeNone {
			continue
		}
		if time.Since(e.createdAt) >= c.beforeSendTimeout {
			c.finish(e, outcomeFailed, c.timeoutError())
			continue
		}
		c.startHook(e)
	}
}

func (c *Client) timeoutError() *Error {
	return &Error{Code: CodeHookTimeout, Message: "BeforeSend did not finish within " + c.beforeSendTimeout.String() + "; the event was dropped."}
}

func (c *Client) startHook(e *entry) {
	// The hook gets a copy of its own: what it does with it changes nothing the client holds.
	decoded, err := jsonv.Decode(e.json)
	object, isObject := decoded.(*jsonv.Object)
	if err != nil || !isObject {
		c.finish(e, outcomeFailed, &Error{Code: CodeInternal, Message: "BeforeSend could not be started; the event was dropped."})
		return
	}
	draft := e.draft
	deadline := e.createdAt.Add(c.beforeSendTimeout)
	// Marked as a callback's: a Flush given this context doesn't wait on the hook's own event, and a
	// capture made in it is one level deeper than the event's.
	ctx, cancel := context.WithDeadline(bridge.MarkCallbackAt(context.Background(), c, e.depth+1), deadline)
	e.stage = stageHook
	e.hookCancel = cancel
	c.activeHooks++
	hook := c.beforeSend
	go func() {
		var out *Event
		failed := true
		// However the hook ends, by returning, panicking or ending its goroutine, its slot comes
		// back, and its time is taken as it ends: what the SDK does next isn't the hook's.
		defer func() {
			_ = recover()
			at := time.Now()
			e.returned.Store(true)
			c.hookReturned(e, draft, out, failed, at)
		}()
		event, err := hook(ctx, publicEvent(object, draft.Limited()))
		out, failed = event, err != nil
	}()
}

// hookReturned takes a hook's answer: its slot comes back first, then what it returned is shaped
// and redacted outside the lock, if the event is still wanted.
func (c *Client) hookReturned(e *entry, draft *core.Draft, out *Event, failed bool, at time.Time) {
	reshape := false
	c.mu.Lock()
	c.guardLocked(func() { reshape = c.hookDone(e, failed, out == nil, at) })
	c.mu.Unlock()
	if !reshape {
		return
	}
	var built *core.Draft
	var failure *core.Failure
	// However the shaping ends, the event is taken out of it: sent, or dropped as broken.
	broken := true
	defer func() {
		_ = recover()
		c.mu.Lock()
		defer c.mu.Unlock()
		c.guardLocked(func() { c.reshaped(e, broken, built, failure) })
	}()
	output := hookOutput
	if draft.Kind == kindTurn {
		output = hookTurnOutput
	}
	built, failure = core.AfterHook(output(out), draft, c.context)
	broken = false
}

// guardLocked is guard for a step that runs with the lock held.
func (c *Client) guardLocked(step func()) {
	defer func() {
		if recover() != nil {
			c.report(&Error{Code: CodeInternal, Message: "The SDK failed unexpectedly; an event may have been dropped."}, nil)
		}
	}()
	step()
}

// hookDone is the end of an event's hook. It reports whether what the hook returned is to be
// shaped: the event is then out of its hook, so the hook's deadline no longer applies to it, and
// not yet ready. Until its shaping is done it holds what it held inside its hook, which nothing
// evicts. Called with the lock held.
func (c *Client) hookDone(e *entry, failed, dropped bool, at time.Time) (reshape bool) {
	c.activeHooks--
	if e.hookCancel != nil {
		e.hookCancel()
		e.hookCancel = nil
	}
	defer c.pumpHooks()
	if e.zombie {
		// Too late: the event was already dropped. Release what the hook held, discard its result.
		c.releaseZombie(e)
		return false
	}
	if e.outcome != outcomeNone {
		return false
	}
	if e.timer != nil {
		e.timer.Stop()
		e.timer = nil
	}
	switch {
	case at.Sub(e.createdAt) >= c.beforeSendTimeout:
		// The deadline's timer may not have run yet: checked here too, by when the hook returned.
		e.stage = stageQueued
		c.finish(e, outcomeFailed, c.timeoutError())
	case failed:
		e.stage = stageQueued
		c.finish(e, outcomeFailed, &Error{Code: CodeHookFailed, Message: "BeforeSend returned an error or panicked; the event was dropped."})
	case dropped:
		e.stage = stageQueued
		c.finish(e, outcomeSkipped, nil)
	default:
		e.stage = stageReshape
		return true
	}
	return false
}

func (c *Client) releaseZombie(e *entry) {
	e.zombie = false
	c.zombies--
	c.zombieBytes -= e.bytes
}

// reshaped takes a hook's output once it is shaped and redacted. Called with the lock held.
func (c *Client) reshaped(e *entry, broken bool, built *core.Draft, failure *core.Failure) {
	if e.zombie {
		// Dropped while its output was shaped: what it held is given back now.
		c.releaseZombie(e)
		return
	}
	if e.outcome != outcomeNone {
		return
	}
	e.stage = stageQueued
	e.draft = nil
	switch {
	case broken:
		c.finish(e, outcomeFailed, &Error{Code: CodeRedactionFailed, Message: "Redaction failed; the event was dropped, not sent unredacted."})
	case failure != nil && failure.Code == "too_large":
		c.finish(e, outcomeFailed, &Error{Code: CodeTooLarge, Message: "BeforeSend made the event larger than " + strconv.Itoa(c.maxEventBytes) + " bytes even without its largest parts; it was dropped."})
	case failure != nil:
		c.finish(e, outcomeFailed, &Error{Code: CodeHookFailed, Message: "BeforeSend returned invalid IDs; the event was dropped."})
	default:
		encoded := jsonv.Marshal(built.Event)
		c.liveBytes += len(encoded) - e.bytes
		e.bytes = len(encoded)
		e.json = encoded
		e.redacted = true
		if !c.fitsAfterGrowth(e) {
			c.finish(e, outcomeFailed, &Error{Code: CodeQueueFull, Message: "The event grew past the memory limits; it was dropped."})
		} else if e.outcome == outcomeNone {
			c.ready(e)
		}
	}
}

// hookDeadline is an event's hook timer: the event is dropped if it is still waiting for its hook
// or inside it.
func (c *Client) hookDeadline(e *entry) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if e.outcome != outcomeNone || (e.stage != stageWaiting && e.stage != stageHook) {
		return
	}
	// Back already, and only waiting for the lock this holds: when it returned decides.
	if e.stage == stageHook && e.returned.Load() {
		return
	}
	c.guardLocked(func() { c.finish(e, outcomeFailed, c.timeoutError()) })
}

// ready is for an event that is redacted and final: logged in a dry run, otherwise queued for the
// next request with a free slot. Called with the lock held.
func (c *Client) ready(e *entry) {
	if e.outcome != outcomeNone {
		return
	}
	if c.mode == modeDryRun {
		c.log("info", dryRunSummary(e))
		c.finish(e, outcomeSkipped, nil)
		return
	}
	c.dispatch(e)
}

// dispatch queues an event to be sent, in the order events became ready. Requests go out from a
// goroutine that takes the lock after this one lets go of it, so events that become ready together
// go together.
func (c *Client) dispatch(e *entry) {
	e.stage = stageQueued
	e.queued = c.queued.PushBack(e)
	c.schedulePump()
}

// schedulePump has requests sent shortly. Called with the lock held.
func (c *Client) schedulePump() {
	if c.pumpScheduled {
		return
	}
	c.pumpScheduled = true
	go func() {
		c.mu.Lock()
		defer c.mu.Unlock()
		c.pumpScheduled = false
		c.guardLocked(c.pumpRequests)
	}()
}

// Sending.

// pumpRequests sends batches while a request slot is free: each carries the events ready now, up
// to the ingestion's limits. The limit on slots keeps a slow or unreachable LSquare from holding
// many connections, and lets batches form under load. Called with the lock held.
func (c *Client) pumpRequests() {
	if c.holdback != nil && !c.holdingBack() {
		return
	}
	for c.activeRequests < c.maxRequests && c.queued.Len() > 0 {
		c.sendBatch(c.takeBatch(), false)
	}
}

// admit says whether an event recorded while a daily limit holds goes ahead, becomes the check, or
// is dropped and counted.
func (c *Client) admit(h *holdback) string {
	now := time.Now()
	if !now.Before(h.until) {
		c.endHoldback()
		return "send"
	}
	if check := h.check; check != nil && !now.Before(h.nextCheck) {
		// Unanswered for a whole interval: dropped and counted, and this event checks instead.
		h.check = nil
		h.dropped++
		c.finish(check, outcomeFailed, nil)
	}
	if h.check == nil && !h.checking && !now.Before(h.nextCheck) {
		// One event at a time is the check, from here until it is queued or dropped.
		h.checking = true
		return "check"
	}
	h.dropped++
	return "drop"
}

// holdingBack drops, while a daily limit holds, the events that became ready (recorded before it,
// or held by their hook), and sends the check on its own. True once the limit has reset.
func (c *Client) holdingBack() bool {
	h := c.holdback
	if !time.Now().Before(h.until) {
		c.endHoldback()
		return true
	}
	for element := c.queued.Front(); element != nil; {
		next := element.Next()
		if e := element.Value.(*entry); e != h.check {
			h.dropped++
			c.finish(e, outcomeFailed, nil)
		}
		element = next
	}
	if check := h.check; check != nil && check.queued != nil && check.stage == stageQueued && c.activeRequests < c.maxRequests {
		c.queued.Remove(check.queued)
		check.queued = nil
		c.sendBatch([]*entry{check}, true)
	}
	return false
}

// startHoldback starts holding back, or extends it, until resetsAt. A resetsAt already past means
// the limit has reset since: a late answer, which starts nothing. False then.
func (c *Client) startHoldback(resetsAt string) bool {
	now := time.Now()
	if parsed, ok := parseTime(resetsAt); ok && !parsed.After(now) {
		return false
	}
	until := resetTime(resetsAt, now)
	at := until.UTC().Format("2006-01-02T15:04:05.000Z")
	if h := c.holdback; h != nil {
		if until.After(h.until) {
			h.until = until
			h.resetsAt = at
		}
		return true
	}
	c.holdback = &holdback{until: until, resetsAt: at, nextCheck: now.Add(quotaProbe)}
	c.report(&Error{Code: CodeQuotaExceeded, Message: "The project's daily limit was reached; events are dropped until " + at[11:16] + " UTC.", ResetsAt: at}, nil)
	return true
}

func (c *Client) endHoldback() {
	h := c.holdback
	c.holdback = nil
	if h != nil && h.dropped > 0 {
		c.report(&Error{Code: CodeQuotaDropped, Message: "Sending resumed after the daily limit; " + strconv.Itoa(h.dropped) + " events were dropped.", Count: h.dropped}, nil)
	}
}

// resetTime is when a daily limit resets: resetsAt if it's in the next 25 hours, else the next
// 00:00 UTC.
func resetTime(resetsAt string, now time.Time) time.Time {
	if parsed, ok := parseTime(resetsAt); ok && parsed.After(now) && !parsed.After(now.Add(25*time.Hour)) {
		return parsed
	}
	utc := now.UTC()
	return time.Date(utc.Year(), utc.Month(), utc.Day()+1, 0, 0, 0, 0, time.UTC)
}

// parseTime reads a time as the ingestion or a proxy may write one, which is as JavaScript's
// Date reads them: RFC 3339 with or without a fraction; without a zone, in local time; a date
// alone, in UTC.
func parseTime(text string) (time.Time, bool) {
	text = strings.TrimSpace(text)
	if parsed, err := time.Parse(time.RFC3339Nano, text); err == nil {
		return parsed, true
	}
	for _, layout := range []string{"2006-01-02T15:04:05.999999999", "2006-01-02T15:04"} {
		if parsed, err := time.Parse(layout, text); err == nil {
			return localTime(parsed, time.Local), true
		}
	}
	if parsed, err := time.Parse("2006-01-02", text); err == nil {
		return parsed, true
	}
	return time.Time{}, false
}

// localTime is the time a clock in loc shows as wall's date and time, as JavaScript reads a time
// written without a zone: where a change of offset skips that time or repeats it, by the offset
// in effect before the change.
func localTime(wall time.Time, loc *time.Location) time.Time {
	year, month, day := wall.Date()
	hour, minute, second := wall.Clock()
	picked := time.Date(year, month, day, hour, minute, second, wall.Nanosecond(), loc)
	shown := time.Date(year, month, day, hour, minute, second, wall.Nanosecond(), time.UTC)
	start, end := picked.ZoneBounds()
	for _, change := range []time.Time{start, end} {
		if change.IsZero() {
			continue
		}
		_, before := change.Add(-time.Second).Zone()
		_, after := change.Zone()
		byBefore := shown.Add(-time.Duration(before) * time.Second)
		byAfter := shown.Add(-time.Duration(after) * time.Second)
		// Repeated: read by either offset, it falls on that offset's side of the change.
		// Skipped: by neither.
		if byBefore.Before(change) == !byAfter.Before(change) {
			return byBefore.In(loc)
		}
	}
	return picked
}

// ackBytes is what one event can add to an acknowledgement: its ID with the longest rejection,
// and a comma.
func ackBytes(eventID string) int {
	return len(`{"eventId":`) + len(jsonv.AppendString(nil, eventID)) + len(`,"reason":"quota_exceeded","resetsAt":"`) + len(longestResetsAt) + len(`"}`) + 1
}

// takeBatch takes the first event ready and the others ready now, in order, within the ingestion's
// limits: 100 events, 1 MB of body, and an acknowledgement that stays under 60 KiB even if every
// event came back as quota_exceeded.
func (c *Client) takeBatch() []*entry {
	var taken []*entry
	body, ack := batchOverhead, ackOverhead
	for element := c.queued.Front(); element != nil; element = c.queued.Front() {
		e := element.Value.(*entry)
		eventAck := ackBytes(e.eventID)
		if len(taken) > 0 && (len(taken) >= core.MaxBatchEvents || body+e.bytes+1 > core.MaxBatchBytes || ack+eventAck > core.MaxAckBytes) {
			break
		}
		c.queued.Remove(element)
		e.queued = nil
		taken = append(taken, e)
		body += e.bytes + 1
		ack += eventAck
	}
	return taken
}

// sendBatch starts one request for the events. Every attempt ends by the request timeout, so the
// slot always returns: by the request's own end, or, with a transport that ignores its context,
// without it, whatever that request answers later being dropped.
func (c *Client) sendBatch(entries []*entry, probe bool) {
	ctx, cancel := context.WithTimeout(context.Background(), c.requestTimeout)
	b := &batch{live: len(entries), cancel: cancel, probe: probe, entries: entries}
	if probe {
		b.holdback = c.holdback
	}
	body := make([]byte, 0, batchOverhead+len(entries))
	body = append(body, `{"events":[`...)
	for i, e := range entries {
		e.stage = stageSending
		e.batch = b
		e.attempts++
		if i > 0 {
			body = append(body, ',')
		}
		body = append(body, e.json...)
	}
	body = append(body, "]}"...)
	c.activeRequests++
	// Too many requests already left behind: this one fails as a network error would, and is
	// retried, without another goroutine that might never end. (The requests in flight can be
	// left behind too, so at most that many more than the limit ever are.)
	if c.abandoned >= max(maxAbandonedRequests, 4*c.maxRequests) {
		go func() {
			cancel()
			c.conclude(b, nil)
		}()
		return
	}
	for _, e := range entries {
		e.exchanged = true
	}
	answers := make(chan *reply, 1)
	go func() {
		var answer *reply
		// However the transport ends, by returning, panicking or ending its goroutine, an answer
		// is given: what waits for this one never waits for ever.
		defer func() { answers <- answer }()
		answer = c.exchange(ctx, body)
	}()
	go func() {
		defer cancel()
		answer, left := await(ctx, answers)
		if left {
			// The transport hasn't returned: it is left behind, and counted until it does.
			c.mu.Lock()
			c.abandoned++
			c.mu.Unlock()
			go func() {
				<-answers
				c.mu.Lock()
				c.abandoned--
				c.mu.Unlock()
			}()
		}
		c.conclude(b, answer)
	}()
}

// await is a request's answer, or nil once its time has run out, or none of its events needs it
// any more. An answer that is there by then is taken all the same: if it is an acknowledgement,
// LSquare has the events, and sending them again would only repeat them. left: there is none
// yet, so the transport is still working on it.
func await(ctx context.Context, answers <-chan *reply) (answer *reply, left bool) {
	select {
	case answer = <-answers:
		return answer, false
	case <-ctx.Done():
		select {
		case answer = <-answers:
			return answer, false
		default:
			return nil, true
		}
	}
}

// conclude ends a request: its answer is taken, and its slot comes back.
func (c *Client) conclude(b *batch, answer *reply) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.guardLocked(func() { c.answered(b, answer) })
	c.activeRequests--
	c.guardLocked(c.pumpRequests)
}

// reply is what a request came to. Nil: a network error, a timeout or a redirect, all retried.
type reply struct {
	status     int
	retryAfter time.Duration
	hasRetry   bool
	// The acknowledgement; ok is false when it was too large or unreadable.
	body []byte
	ok   bool
}

// exchange is one request and its answer. It never panics: a transport that does counts as a
// network error.
func (c *Client) exchange(ctx context.Context, body []byte) (answer *reply) {
	defer func() {
		if recover() != nil {
			answer = nil
		}
	}()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return nil
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+c.apiKey)
	response, err := c.http.Do(request)
	if err != nil {
		return nil
	}
	defer response.Body.Close()
	status := response.StatusCode
	// Any 3xx is refused, and retried as a network error: a redirect is never followed.
	if status >= 300 && status < 400 {
		return nil
	}
	answer = &reply{status: status}
	answer.retryAfter, answer.hasRetry = parseRetryAfter(response.Header.Get("Retry-After"), time.Now())
	// Only a 2xx body matters (the acknowledgement). Others are decided from status and headers.
	if status >= 200 && status < 300 {
		read, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
		if err != nil {
			// An acknowledgement that stopped short is no answer: retried, as a network error is.
			return nil
		}
		// One larger than is read is an answer, which says nothing of any event.
		if len(read) <= maxResponseBytes {
			answer.body, answer.ok = read, true
		}
	}
	return answer
}

// parseRetryAfter reads a Retry-After header: seconds, or a date.
func parseRetryAfter(header string, now time.Time) (time.Duration, bool) {
	header = strings.TrimSpace(header)
	if header == "" {
		return 0, false
	}
	digits := true
	for i := 0; i < len(header); i++ {
		digits = digits && header[i] >= '0' && header[i] <= '9'
	}
	if digits {
		seconds, err := strconv.ParseInt(header, 10, 64)
		if err != nil || seconds > int64(retryAfterCap/time.Second) {
			return retryAfterCap, true
		}
		return time.Duration(seconds) * time.Second, true
	}
	date, err := http.ParseTime(header)
	if err != nil {
		parsed, ok := parseTime(header)
		if !ok {
			return 0, false
		}
		date = parsed
	}
	return max(date.Sub(now), 0), true
}

// answered takes a request's answer. The events still this request's are those it carries that
// nothing finished meanwhile: a late answer finishes, retries or holds back nothing else. Called
// with the lock held.
func (c *Client) answered(b *batch, answer *reply) {
	var live []*entry
	for _, e := range b.entries {
		if e.batch == b {
			e.batch = nil
			if e.outcome == outcomeNone {
				live = append(live, e)
			}
		}
	}
	if answer == nil {
		c.retryOrFail(live, 0, 0, false)
		return
	}
	status := answer.status
	switch {
	case status >= 200 && status < 300:
		var acks map[string]ack
		if answer.ok {
			acks = acknowledgements(answer.body)
		}
		var overQuota []*entry
		resetsAt := ""
		accepted := 0
		for _, e := range live {
			result, named := acks[e.eventID]
			switch {
			case named && result.accepted:
				accepted++
				c.finish(e, outcomeSent, nil)
			case !named || result.unexpected:
				c.finish(e, outcomeFailed, &Error{Code: CodeSendFailed, Message: "Unexpected response: LSquare did not acknowledge the event.", Status: status})
			case result.rejected == "quota_exceeded":
				if resetsAt == "" {
					resetsAt = result.resetsAt
				}
				overQuota = append(overQuota, e)
			default:
				c.finish(e, outcomeFailed, &Error{Code: CodeRejected, Message: "LSquare rejected the event: " + result.rejected + "."})
			}
		}
		if len(overQuota) > 0 {
			// Final, never retried. Holding back, they're counted and reported once; after a late
			// answer from a limit that has reset since, each is reported.
			holding := c.startHoldback(resetsAt)
			for _, e := range overQuota {
				if holding && c.holdback != nil {
					c.holdback.dropped++
					c.finish(e, outcomeFailed, nil)
				} else {
					c.finish(e, outcomeFailed, &Error{Code: CodeRejected, Message: "LSquare rejected the event: quota_exceeded."})
				}
			}
		} else if b.probe && accepted > 0 && c.holdback != nil && c.holdback == b.holdback {
			c.endHoldback()
		}
	case status == 429 || status >= 500:
		c.retryOrFail(live, status, answer.retryAfter, answer.hasRetry)
	default:
		for _, e := range live {
			c.finish(e, outcomeFailed, &Error{Code: CodeSendFailed, Message: "LSquare answered " + strconv.Itoa(status) + "; not retried.", Status: status})
		}
	}
}

// ack is what an acknowledgement says of one event.
type ack struct {
	accepted bool
	// Named as both accepted and rejected, or rejected twice: it says nothing certain.
	unexpected bool
	rejected   string
	resetsAt   string
}

// acknowledgements reads an acknowledgement. Each event counts as sent only if LSquare names it as
// accepted or duplicate, and not also as rejected. Nil when the body isn't an acknowledgement at all.
func acknowledgements(body []byte) map[string]ack {
	// Without a byte order mark, which a decoder of text takes off.
	decoded, err := jsonv.Decode(bytes.TrimPrefix(body, []byte("\xEF\xBB\xBF")))
	if err != nil {
		return nil
	}
	object, ok := decoded.(*jsonv.Object)
	if !ok || object == nil {
		return nil
	}
	acks := map[string]ack{}
	for _, key := range []string{"accepted", "duplicates"} {
		value, _ := object.Get(key)
		list, _ := value.([]any)
		for _, id := range list {
			if text, ok := id.(string); ok {
				acks[text] = ack{accepted: true}
			}
		}
	}
	value, _ := object.Get("rejected")
	list, _ := value.([]any)
	for _, item := range list {
		rejection, ok := item.(*jsonv.Object)
		if !ok || rejection == nil {
			continue
		}
		id, ok := fieldText(rejection, "eventId")
		if !ok {
			continue
		}
		if _, named := acks[id]; named {
			acks[id] = ack{unexpected: true}
			continue
		}
		reason, _ := fieldText(rejection, "reason")
		if !rejectionReasons[reason] {
			reason = "unspecified"
		}
		resetsAt, _ := fieldText(rejection, "resetsAt")
		acks[id] = ack{rejected: reason, resetsAt: resetsAt}
	}
	return acks
}

// retryOrFail retries each event on its own count, with one delay for the batch, so its events go
// out together again. status 0: a network error.
func (c *Client) retryOrFail(entries []*entry, status int, retryAfter time.Duration, hasRetryAfter bool) {
	var retrying []*entry
	attempts := 0
	for _, e := range entries {
		if e.attempts < c.maxAttempts {
			retrying = append(retrying, e)
			attempts = max(attempts, e.attempts)
			continue
		}
		failure := &Error{Code: CodeSendFailed, Message: "Network error after " + strconv.Itoa(e.attempts) + " attempts."}
		if status != 0 {
			failure.Message = "LSquare answered " + strconv.Itoa(status) + " after " + strconv.Itoa(e.attempts) + " attempts."
			failure.Status = status
		}
		c.finish(e, outcomeFailed, failure)
	}
	if len(retrying) == 0 {
		return
	}
	// 250 ms doubled per attempt, 5 s at most: past the fifth attempt it is the cap.
	ceiling := retryCap
	if attempts <= 5 {
		ceiling = min(retryCap, retryBase<<uint(attempts-1))
	}
	delay := time.Duration(rand.Float64() * float64(ceiling))
	if hasRetryAfter {
		delay = min(retryAfter, retryAfterCap)
	}
	retryAt := time.Now().Add(delay)
	c.retryTokens++
	token := c.retryTokens
	// One timer for them all, stopped if none of them waits for it any more.
	group := &retryGroup{waiting: len(retrying)}
	for _, e := range retrying {
		e.stage = stageRetry
		e.retryAt = retryAt
		e.retryToken = token
		e.retry = group
	}
	group.timer = time.AfterFunc(delay, func() { c.retryNow(retrying, token) })
	// A flush whose deadline comes first can't see these events finish: it stops waiting for them.
	c.broadcast()
}

// retryNow is a batch's retry coming due: its events that are still waiting for it are queued
// again, together.
func (c *Client) retryNow(entries []*entry, token uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.guardLocked(func() {
		for _, e := range entries {
			if e.retryToken != token || e.outcome != outcomeNone || e.stage != stageRetry {
				continue
			}
			e.retryToken = 0
			e.retry = nil
			e.stage = stageQueued
			e.queued = c.queued.PushBack(e)
		}
		c.pumpRequests()
	})
}

// The queue's limits.

// held is what the queue's limits count: the events it holds, those dropped while their hook
// still runs, and the calls whose events are still being built.
func (c *Client) held() (count, bytes int) {
	return c.live.Len() + c.zombies + c.finishingHeld, c.liveBytes + c.zombieBytes + c.finishingBytes
}

// pinned is what eviction can't free: expired hooks still running, events inside a running hook,
// and calls whose events are still being built.
func (c *Client) pinned(except *entry) (count, bytes int) {
	count, bytes = c.zombies+c.finishingHeld, c.zombieBytes+c.finishingBytes
	for element := c.live.Front(); element != nil; element = element.Next() {
		if e := element.Value.(*entry); (e.stage == stageHook || e.stage == stageReshape) && e != except {
			count++
			bytes += e.bytes
		}
	}
	return count, bytes
}

// makeRoom makes room for a new event by dropping the oldest ones, unless even that couldn't make
// it fit.
func (c *Client) makeRoom(bytes int) bool {
	if bytes < 0 || bytes > c.maxQueueBytes {
		return false
	}
	// Most often there is room: nothing of the queue is walked then.
	if c.fits(bytes) {
		return true
	}
	count, held := c.pinned(nil)
	if count+1 > c.maxQueueEvents || held > c.maxQueueBytes-bytes {
		return false
	}
	return c.evictUntil(func() bool { return c.fits(bytes) }, nil)
}

// fits reports whether one more event, or call being built, of this size is within the limits
// as the queue stands.
func (c *Client) fits(bytes int) bool {
	count, held := c.held()
	return bytes >= 0 && bytes <= c.maxQueueBytes && count+1 <= c.maxQueueEvents && held <= c.maxQueueBytes-bytes
}

// fitsAfterGrowth drops older events, after one grew (its hook's output, placeholders), until the
// budget holds again.
func (c *Client) fitsAfterGrowth(grown *entry) bool {
	if _, held := c.held(); held <= c.maxQueueBytes {
		return true
	}
	if _, held := c.pinned(grown); grown.bytes > c.maxQueueBytes || held > c.maxQueueBytes-grown.bytes {
		return false
	}
	return c.evictUntil(func() bool {
		_, held := c.held()
		return held <= c.maxQueueBytes
	}, grown)
}

// evictUntil drops the oldest events until fits. Events inside a hook free nothing, so they are
// skipped. Events with a request in flight go last: cancelling them may not stop LSquare
// receiving them.
func (c *Client) evictUntil(fits func() bool, keep *entry) bool {
	if fits() {
		return true
	}
	for _, inFlight := range []bool{false, true} {
		for element := c.live.Front(); element != nil; {
			next := element.Next()
			e := element.Value.(*entry)
			if e != keep && e.stage != stageHook && e.stage != stageReshape && (e.stage == stageSending) == inFlight {
				message := "Dropped to stay within the memory limits."
				if inFlight {
					message = "Dropped to stay within the memory limits; it was in a request already, and may still have reached LSquare."
				}
				c.finish(e, outcomeFailed, &Error{Code: CodeQueueFull, Message: message})
				if fits() {
					return true
				}
			}
			element = next
		}
	}
	return false
}

// finish is an event's final state. Anything that happens to it later (a late hook, a response) is
// ignored. Called with the lock held.
func (c *Client) finish(e *entry, result outcome, failure *Error) {
	if e.outcome != outcomeNone {
		return
	}
	e.outcome = result
	e.done.Store(true)
	e.retryToken = 0
	if group := e.retry; group != nil {
		e.retry = nil
		if group.waiting--; group.waiting == 0 {
			group.timer.Stop()
		}
	}
	b := e.batch
	e.batch = nil
	// Its request carries other events too: cancelled only once none of them still needs it.
	if b != nil {
		b.live--
		if b.live == 0 {
			b.cancel()
		}
	}
	if e.stage == stageHook || e.stage == stageReshape {
		// Its hook is still running, or what it returned is still being shaped: it keeps its bytes
		// (and, in its hook, its slot) until that ends, and the hook is told to stop.
		e.zombie = true
		c.zombies++
		c.zombieBytes += e.bytes
		if e.hookCancel != nil {
			e.hookCancel()
		}
	}
	if e.timer != nil {
		e.timer.Stop()
		e.timer = nil
	}
	c.live.Remove(e.live)
	if e.queued != nil {
		if e.stage == stageWaiting {
			c.waiting.Remove(e.queued)
		} else {
			c.queued.Remove(e.queued)
		}
		e.queued = nil
	}
	c.liveBytes -= e.bytes
	if h := c.holdback; h != nil && h.check == e {
		h.check = nil
		if !e.exchanged {
			// The check was never sent (its hook dropped it, room ran out, or no request could be
			// started for it): the next event checks instead, where waiting out the interval would
			// put recovery off by as long.
			h.nextCheck = time.Now()
		}
	}
	if failure != nil {
		failure.EventID = e.eventID
		var event []byte
		if e.redacted {
			event = e.json
		}
		c.report(failure, event)
	}
	// Whatever still holds this entry (a flush, a slow request, a hook that ignored its context)
	// holds no content from here.
	e.json = nil
	e.draft = nil
	c.broadcast()
}

// Waiting.

// waitFor waits until every entry is finished, or can't be by the deadline (its next attempt is
// due after it), or the deadline or done. Called with the lock held; it is released while
// waiting.
func (c *Client) waitFor(done <-chan struct{}, entries []*entry, deadline time.Time) {
	for {
		pending := false
		for _, e := range entries {
			if e.outcome == outcomeNone && !(e.stage == stageRetry && e.retryAt.After(deadline)) {
				pending = true
				break
			}
		}
		left := time.Until(deadline)
		if !pending || left <= 0 || isDone(done) {
			return
		}
		changed := c.changed
		c.mu.Unlock()
		timer := time.NewTimer(left)
		select {
		case <-changed:
		case <-timer.C:
		case <-done:
		}
		timer.Stop()
		c.mu.Lock()
	}
}

// finishCalls waits for calls whose events are still being built, until the deadline or done.
// Called with the lock held; it is released while waiting.
func (c *Client) finishCalls(done <-chan struct{}, calls []*finishingCall, deadline time.Time) {
	c.mu.Unlock()
	defer c.mu.Lock()
	timer := time.NewTimer(max(time.Until(deadline), 0))
	defer timer.Stop()
	for _, call := range calls {
		select {
		case <-call.done:
		case <-timer.C:
			return
		case <-done:
			return
		}
	}
}

// doneOf is ctx's Done channel, taken once, and before the client's lock is: what waits with the
// lock held, or gives it back to wait, then runs none of a caller's context's code, which could
// block, take the client's lock itself or panic. One whose Done panics is waited on until the
// deadline only.
func doneOf(ctx context.Context) (done <-chan struct{}) {
	defer func() {
		if recover() != nil {
			done = nil
		}
	}()
	return ctx.Done()
}

// isDone reports whether done is closed: never for nil.
func isDone(done <-chan struct{}) bool {
	select {
	case <-done:
		return true
	default:
		return false
	}
}

func (c *Client) liveEntries() []*entry {
	entries := make([]*entry, 0, c.live.Len())
	for element := c.live.Front(); element != nil; element = element.Next() {
		entries = append(entries, element.Value.(*entry))
	}
	return entries
}

func (c *Client) finishingCalls() []*finishingCall {
	calls := make([]*finishingCall, 0, len(c.finishing))
	for _, call := range c.finishing {
		calls = append(calls, call)
	}
	sort.Slice(calls, func(a, b int) bool { return calls[a].callID < calls[b].callID })
	return calls
}

// addCalls adds the events these calls queued to targets, finished or not. It returns the calls
// still unbuilt, and how many were dropped before they were queued.
func addCalls(targets []*entry, calls []*finishingCall) ([]*entry, []*finishingCall, int) {
	seen := make(map[*entry]bool, len(targets))
	for _, e := range targets {
		seen[e] = true
	}
	var unbuilt []*finishingCall
	dropped := 0
	for _, call := range calls {
		switch {
		case call.entry != nil:
			if !seen[call.entry] {
				seen[call.entry] = true
				targets = append(targets, call.entry)
			}
		case call.finished:
			dropped++
		default:
			unbuilt = append(unbuilt, call)
		}
	}
	return targets, unbuilt, dropped
}

func count(result *FlushResult, e *entry) bool {
	switch e.outcome {
	case outcomeSent:
		result.Sent++
	case outcomeSkipped:
		result.Skipped++
	case outcomeFailed:
		result.Failed++
	default:
		return false
	}
	return true
}

// Flush waits for the events recorded before the call to be sent: until ctx's deadline, or for
// Options.FlushTimeout when it has none. It then gives OnError and the Logger a moment more (50
// ms, when that time is up) to be told of what it reported. An event whose next retry is after that isn't waited
// for. Events still pending when it returns are reported to OnError. Call it before a process
// that can't call Shutdown goes idle, such as at the end of a serverless request.
//
// Called from BeforeSend or Identify with the context they were given, it waits only for events
// that are past BeforeSend, and counts no others: an event still waiting for its hook, or a call
// whose event is still being built, may be the caller's own, or be waiting for the caller to
// finish. Called from OnError, it reports nothing as pending, which OnError would be told of
// again.
func (c *Client) Flush(ctx context.Context) (result FlushResult) {
	if c == nil {
		return result
	}
	if ctx == nil {
		ctx = context.Background()
	}
	deadline, bounded := ctx.Deadline()
	if !bounded {
		deadline = time.Now().Add(c.flushTimeout)
	}
	// Inside a tool call: the call's own event, which isn't built until the tool returns, is
	// sent before the reply instead (see bridge.Client.Settle). What OnError and the Logger are
	// told isn't waited for there: the reply would wait on them.
	inTool := c.inTool(ctx)
	inCallback := bridge.InCallback(ctx, c)
	// Its own call's event isn't Flush's to wait for: the reply is.
	own := func(eventID string) bool { return inTool != "" && eventID == inTool }
	// Last, with the lock released: what was reported, and a disabled client's warning, have
	// reached OnError or the Logger when it returns. Not from BeforeSend or Identify: the handler
	// it would wait for may be waiting for them.
	defer func() {
		_ = recover()
		if !inCallback && inTool == "" {
			c.reports.wait(ctx, deadline)
		}
	}()
	if c.mode == modeDisabled {
		return result
	}
	inHandler := c.reports.inHandler()
	// As asked: what is left pending is reported even when finishing calls took all the time.
	waits := time.Until(deadline) > 0
	done := doneOf(ctx)
	c.mu.Lock()
	defer c.mu.Unlock()
	targets := slices.DeleteFunc(c.liveEntries(), func(e *entry) bool { return own(e.eventID) })
	var unbuilt []*finishingCall
	dropped := 0
	if inCallback {
		targets = slices.DeleteFunc(targets, func(e *entry) bool { return e.stage == stageWaiting || e.stage == stageHook })
	} else {
		// Calls that have replied but whose events aren't built yet count too: waited for first.
		calls := slices.DeleteFunc(c.finishingCalls(), func(call *finishingCall) bool { return own(call.callID) })
		if len(calls) > 0 && waits && !inHandler {
			c.finishCalls(done, calls, deadline)
		}
		targets, unbuilt, dropped = addCalls(targets, calls)
	}
	result.Pending += len(unbuilt)
	result.Failed += dropped
	c.pumpHooks()
	c.pumpRequests()
	c.waitFor(done, targets, deadline)
	for _, e := range targets {
		if count(&result, e) {
			continue
		}
		result.Pending++
		if waits && !inHandler {
			var event []byte
			if e.redacted {
				event = e.json
			}
			c.report(&Error{Code: CodePendingAfterFlush, Message: "Still pending when Flush returned.", EventID: e.eventID}, event)
		}
	}
	if waits && !inHandler {
		for _, call := range unbuilt {
			c.report(&Error{Code: CodePendingAfterFlush, Message: "Still pending when Flush returned.", EventID: call.callID}, nil)
		}
	}
	return result
}

// Shutdown ends the client: it drops events still waiting for BeforeSend or inside it, waits for
// the rest (until ctx's deadline, or 5 seconds, or Options.FlushTimeout when that is longer),
// then drops whatever is left, cancelling requests and retries. Calls recorded afterwards are
// dropped. Its result counts the events outstanding when it was called. Call it before the
// process exits.
//
// Called from BeforeSend or Identify with the context they were given, it waits until that
// context's deadline: the context itself ends as Shutdown drops the caller's own event.
func (c *Client) Shutdown(ctx context.Context) (result FlushResult) {
	if c == nil {
		return result
	}
	if ctx == nil {
		ctx = context.Background()
	}
	deadline, bounded := ctx.Deadline()
	if !bounded {
		deadline = time.Now().Add(max(c.flushTimeout, defaultShutdownTimeout))
	}
	defer func() {
		_ = recover()
		c.reports.wait(ctx, deadline)
	}()
	inCallback := bridge.InCallback(ctx, c) || c.reports.inHandler()
	if bridge.InCallback(ctx, c) && ctx.Err() == nil {
		// Called from BeforeSend or Identify with their context, which ends as soon as Shutdown
		// drops the caller's own event: what it waits for next is waited for until the same
		// deadline, not until that. (So a context derived from it and cancelled while Shutdown
		// waits doesn't cut the wait short; one cancelled before the call does.)
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(context.WithoutCancel(ctx), deadline)
		defer cancel()
	}
	done := doneOf(ctx)
	c.mu.Lock()
	defer c.mu.Unlock()
	before := c.liveEntries()
	// Calls whose events are still being built get the chance to be recorded first; one still
	// unbuilt is dropped, so it counts as failed, as does one whose event was refused.
	calls := c.finishingCalls()
	// No new call is taken from here: only the ones already replied to are waited for.
	c.closing = true
	if len(calls) > 0 && time.Until(deadline) > 0 && !inCallback {
		c.finishCalls(done, calls, deadline)
	}
	c.closed = true
	seen := make(map[*entry]bool, len(before))
	targets := before
	for _, e := range before {
		seen[e] = true
	}
	for _, e := range c.liveEntries() {
		if !seen[e] {
			targets = append(targets, e)
		}
	}
	targets, unbuilt, dropped := addCalls(targets, calls)
	result.Failed += len(unbuilt) + dropped
	for _, e := range targets {
		if e.outcome == outcomeNone && (e.stage == stageWaiting || e.stage == stageHook) {
			c.finish(e, outcomeFailed, &Error{Code: CodeShutdown, Message: "Shutdown dropped an event waiting for BeforeSend."})
		}
	}
	c.pumpRequests()
	c.waitFor(done, targets, deadline)
	for _, e := range c.liveEntries() {
		c.finish(e, outcomeFailed, &Error{Code: CodeShutdown, Message: "Shutdown ended before the event was sent."})
	}
	for _, e := range targets {
		if !count(&result, e) {
			result.Failed++
		}
	}
	return result
}

// dryRunSummary is one line per event: its shape only, never its content.
func dryRunSummary(e *entry) string {
	decoded, _ := jsonv.Decode(e.json)
	event, _ := decoded.(*jsonv.Object)
	summary := jsonv.NewObject()
	kind, _ := fieldText(event, "kind")
	summary.Set("kind", kind)
	// Where the ID came from, never the ID: a turn's is made from its conversation and turn IDs.
	switch eventID, _ := fieldText(event, "eventId"); {
	case kind != kindTurn:
		summary.Set("eventId", "the call")
	case strings.HasPrefix(eventID, "k/"):
		summary.Set("eventId", "from turnId, hashed")
	default:
		summary.Set("eventId", "random: no turnId")
	}
	var fields []string
	for _, key := range event.Keys() {
		if !core.SDKFields[key] {
			fields = append(fields, key)
		}
	}
	sort.Strings(fields)
	summary.Set("fields", fields)
	summary.Set("bytes", jsonv.Int(int64(e.bytes)))
	// How many tools the agent called, never which.
	calls, _ := event.Get("toolCalls")
	if kind != kindTurn {
		agent, _ := event.Get("agent")
		calls = nil
		if body, ok := agent.(*jsonv.Object); ok {
			calls, _ = body.Get("toolCalls")
		}
	}
	if list, ok := calls.([]any); ok {
		summary.Set("toolCalls", jsonv.Int(int64(len(list))))
	}
	for _, key := range []string{"truncated", "dropped", "redactions"} {
		if value, ok := event.Get(key); ok {
			summary.Set(key, value)
		}
	}
	if e.ignoredFields.Len() > 0 {
		summary.Set("ignoredFields", e.ignoredFields)
	}
	if e.ignoredToolCallFields.Len() > 0 {
		summary.Set("ignoredToolCallFields", e.ignoredToolCallFields)
	}
	return "[lsquare dry-run] " + string(jsonv.Marshal(summary))
}

// inTool is the ID of the tool call ctx was given to, on a server instrumented with this client,
// whose tool is still running, from the app's own code: the call's reply is then made to wait for
// its event. "" otherwise: a callback of the clients' marks no call, nor does an OnError or a
// Logger given a tool's context it kept.
func (c *Client) inTool(ctx context.Context) string {
	if bridge.CallbackDepth(ctx) > 0 || inAnyHandler() {
		return ""
	}
	if call := bridge.CallOf(ctx, c.owner); call != nil && call.Flush() {
		return call.ID
	}
	return ""
}
