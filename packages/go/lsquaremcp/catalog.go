package lsquaremcp

// Which tools get the extras, read from what the server lists. The MCP SDK doesn't say what
// tools a server has, so the wrapper learns each tool from the server's own listings: the ones
// its clients ask for, as they pass, and one it asks for itself when a call comes before it has
// seen a whole one. A tool's listed schema speaks for the tool only where it refuses the inputs
// it doesn't name: any other tool is left exactly as it is. Nothing is ever added to the server,
// or changed in it: what clients are listed is a copy, and so is a call that is passed on
// without the inputs.

import (
	"context"
	"encoding/json"
	"maps"
	"slices"
	"strconv"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Extras turns off, each by itself, what the wrapper adds to a server's tools. The zero value
// leaves all three on.
type Extras struct {
	// DisableSessions: tools get no session_id input and no result gets a task ID line, and calls
	// aren't grouped by their connection either.
	DisableSessions bool
	// DisableIntent: tools get no intent input.
	DisableIntent bool
	// DisableMissingTools: the server's listing doesn't get the get_more_tools tool.
	DisableMissingTools bool
	// OpenSchemas also gives the inputs to tools whose schema doesn't refuse the inputs it
	// doesn't name: a schema written by hand, without "additionalProperties": false. The wrapper
	// then takes session_id and intent out of those tools' calls on their schema's word, so a
	// tool that reads one of them without its schema saying so no longer gets it.
	OpenSchemas bool
}

// any reports whether any extra is on.
func (e Extras) any() bool {
	return !e.DisableSessions || !e.DisableIntent || !e.DisableMissingTools
}

const (
	// How many pages the wrapper reads of a listing it asks for itself.
	listPages = 100
	// How long after that listing began a call still waits for it, before it goes on as it came,
	// and how long the listing itself may take.
	listWait    = time.Second
	listTimeout = 10 * time.Second
	// How long after such a listing failed the wrapper asks for no other.
	listPause = time.Minute
)

// plan is what the wrapper knows of a listed tool.
type plan struct {
	// The tool as the server listed it: another tool under the same name is read anew.
	tool *mcp.Tool
	// Read: until then only the tool is known.
	read bool
	// The inputs the wrapper takes out of the tool's calls, and shows in its listing.
	sessions, intents bool
	// The tool as clients are listed it, with the inputs in its schema; nil when it is listed
	// as it is.
	listed *mcp.Tool
	// The call is to get_more_tools, and the wrapper answers it.
	answers bool
}

// catalog is the tools the server has listed, by name. Under the wrapper's lock.
type catalog struct {
	plans map[string]*plan
	// A whole listing was read: a name the catalog doesn't have isn't a tool of the server's, as
	// far as any listing said.
	whole bool
	// Counts the whole listings read, and the ones begun: a listing the wrapper asked for that
	// ends after a newer one was read is not the server's tools any more.
	generation int
	// A listing the wrapper asked for is under way, since started: closed when it ends.
	reading chan struct{}
	started time.Time
	// When a listing the wrapper asked for last failed.
	failed time.Time
}

// planOf is what the wrapper knows of the tool a call names: nil for a tool it has never seen
// listed.
func (w *instrumented) planOf(name string) *plan {
	w.mu.Lock()
	entry := w.catalog.plans[name]
	w.mu.Unlock()
	if entry == nil {
		return nil
	}
	return w.reading(name, entry)
}

// reading is the tool's plan, read: a tool the wrapper only saw in a listing of its own is read
// when a call first needs it.
func (w *instrumented) reading(name string, entry *plan) *plan {
	w.mu.Lock()
	if entry.read {
		w.mu.Unlock()
		return entry
	}
	tool := entry.tool
	w.mu.Unlock()
	// Read with nothing held: a schema writes itself as JSON, which can be the app's code.
	fresh := w.read(tool)
	w.mu.Lock()
	defer w.mu.Unlock()
	if current := w.catalog.plans[name]; current != nil && current.tool == tool {
		if !current.read {
			w.catalog.plans[name] = fresh
			return fresh
		}
		return current
	}
	return fresh
}

// read decides what a listed tool leaves to the wrapper, from its schema as listed: nothing,
// unless the schema is an object of named properties that refuses any other (or, with
// OpenSchemas, doesn't say) and can be listed with the inputs; and each input only where the
// tool doesn't name it itself.
func (w *instrumented) read(tool *mcp.Tool) *plan {
	out := &plan{tool: tool, read: true}
	sessions, intents := !w.extras.DisableSessions, !w.extras.DisableIntent
	if (!sessions && !intents) || tool.Name == missingTools {
		// A get_more_tools of the server's own is the server's, with all it takes.
		return out
	}
	label := strconv.Quote(tool.Name)
	alone := func(why string) *plan {
		w.client.WarnOnce("extras-unreadable/"+tool.Name, "[lsquare] Tool "+label+" gets no session_id or intent: its input schema can't be read safely ("+why+"), so any input could be its own.")
		return out
	}
	schema, ok := schemaJSON(tool.InputSchema)
	if !ok {
		return alone("it could not be read")
	}
	own := listedOwnership(schema)
	switch {
	case !own.readable:
		return alone(own.why)
	case !own.closed && !w.extras.OpenSchemas:
		return alone(`it doesn't say "additionalProperties": false`)
	}
	var names []string
	takes := plan{}
	for _, input := range []struct {
		on, declared bool
		name         string
		takes        *bool
	}{{sessions, own.session, sessionInput, &takes.sessions}, {intents, own.intent, intentInput, &takes.intents}} {
		switch {
		case !input.on:
		case input.declared:
			w.client.WarnOnce("extras-own/"+input.name+"/"+tool.Name, "[lsquare] Tool "+label+" has its own "+input.name+" input: it is left as it is.")
		default:
			names = append(names, input.name)
			*input.takes = true
		}
	}
	if len(names) == 0 {
		return out
	}
	// Only where the schema takes one more property without changing what it accepts: an input
	// that can't be shown isn't taken out either, and no agent is told to send it.
	with := advertise(schema, names)
	if with == nil {
		return alone("it can't take one more input without changing what it accepts")
	}
	listed := *tool
	listed.InputSchema = json.RawMessage(with)
	out.sessions, out.intents, out.listed = takes.sessions, takes.intents, &listed
	return out
}

// schemaJSON is a listed schema as the SDK writes it to clients. Its code can be the app's.
func schemaJSON(schema any) (out []byte, ok bool) {
	defer func() {
		if recover() != nil {
			out, ok = nil, false
		}
	}()
	if schema == nil {
		return nil, false
	}
	out, err := json.Marshal(schema)
	return out, err == nil
}

// note reads the tools of a listing into the catalog, each to be read anew: a tool listed again
// may be another tool, or the same one with another schema. whole: the tools are the whole
// listing, so a name that isn't among them is let go. since, when it isn't negative, is the
// generation the listing was begun under: one read since makes this one out of date, and it is
// dropped.
func (w *instrumented) note(tools []*mcp.Tool, whole bool, since int) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if since >= 0 && since != w.catalog.generation {
		return
	}
	before := w.catalog.plans
	if whole || before == nil {
		w.catalog.plans = make(map[string]*plan, min(len(tools), maxTools))
		if !whole {
			maps.Copy(w.catalog.plans, before)
		}
	}
	for _, tool := range tools {
		if tool == nil || tool.Name == "" {
			continue
		}
		if _, has := w.catalog.plans[tool.Name]; has || len(w.catalog.plans) < maxTools {
			w.catalog.plans[tool.Name] = &plan{tool: tool}
		} else {
			// More tools than the catalog holds: it isn't the whole of them.
			whole = false
		}
	}
	if whole {
		w.catalog.whole = true
		w.catalog.generation++
	}
}

// onList is one tools/list. The server's answer goes back as it came, but for the inputs the
// wrapper adds to the schemas of the tools that take them, and get_more_tools at the end of its
// last page, in a copy: nothing of the server's is changed.
func (w *instrumented) onList(ctx context.Context, next mcp.MethodHandler, method string, request mcp.Request) (mcp.Result, error) {
	result, err := next(ctx, method, request)
	if err != nil {
		return result, err
	}
	page, ok := result.(*mcp.ListToolsResult)
	if !ok || page == nil {
		return result, err
	}
	reply := result
	w.quietly(func() {
		w.describe(page.Tools)
		if !w.extras.any() {
			return
		}
		asked, _ := request.(*mcp.ListToolsRequest)
		first := asked == nil || asked.Params == nil || asked.Params.Cursor == ""
		last := page.NextCursor == ""
		if first && !last {
			// The first page of several: what was known of the whole listing is out of date
			// until its last page, where the whole of it is read again.
			w.mu.Lock()
			w.catalog.whole = false
			w.catalog.generation++
			w.mu.Unlock()
		}
		w.note(page.Tools, first && last, -1)
		// After Shutdown nothing is recorded, so nothing more is offered. The catalog still
		// follows the listings: calls still have their tool's inputs taken out.
		if w.client.Ended() {
			return
		}
		tools := slices.Clone(page.Tools)
		changed, own := false, false
		for i, tool := range page.Tools {
			if tool == nil || tool.Name == "" {
				continue
			}
			own = own || tool.Name == missingTools
			if entry := w.planOf(tool.Name); entry != nil && entry.tool == tool && entry.listed != nil {
				tools[i], changed = entry.listed, true
			}
		}
		if own && !w.extras.DisableMissingTools {
			w.client.WarnOnce("extras-own/get_more_tools", "[lsquare] The server has its own "+missingTools+" tool: it is left as it is, and LSquare's isn't added.")
		}
		if last && !own && !w.extras.DisableMissingTools {
			if !first && asked != nil {
				// The last page of several: the earlier ones may have gone to another instance
				// of the server, so the whole listing is read here before the tool is offered.
				w.discover(ctx, next, asked.Session, asked.Extra, asked.Params.Meta)
			}
			if w.offers() {
				if w.choose(w.options.Inputs, "Inputs", missingTools) {
					tools, changed = append(tools, missingToolsTool(!w.extras.DisableSessions)), true
				} else {
					w.client.WarnOnce("extras-no-inputs/"+missingTools, "[lsquare] "+missingTools+" isn't offered: inputs are off for it, so what an agent asks for wouldn't be recorded.")
				}
			}
		}
		if !changed {
			return
		}
		copied := *page
		copied.Tools = tools
		// The SDK adds to a reply's metadata as it sends it.
		copied.Meta = maps.Clone(page.Meta)
		reply = &copied
	})
	return reply, err
}

// offers reports whether the server gets get_more_tools: the extra is on, the server has a tool
// of its own, and none of that name, as far as its whole listing said. A server with its own
// keeps it, and never gets LSquare's.
func (w *instrumented) offers() bool {
	if w.extras.DisableMissingTools {
		return false
	}
	w.mu.Lock()
	whole, tools := w.catalog.whole, len(w.catalog.plans)
	_, own := w.catalog.plans[missingTools]
	w.mu.Unlock()
	if own {
		w.client.WarnOnce("extras-own/get_more_tools", "[lsquare] The server has its own "+missingTools+" tool: it is left as it is, and LSquare's isn't added.")
		return false
	}
	return whole && tools > 0
}

// answer is the wrapper's reply to get_more_tools. Where the SDK says of a tool's own result
// that it is the last one, this one says so too: for a client on protocol 2026-07-28 or later,
// which is the one the connection's initialize asked for, and the latest without one.
func answer(arguments []byte, session *mcp.ServerSession) *mcp.CallToolResult {
	reply := missingToolsReply(arguments)
	if session != nil {
		if asked := session.InitializeParams(); asked != nil && asked.ProtocolVersion < sessionlessProtocol {
			return reply
		}
	}
	// Read back from its JSON, which is the one way to give a result that mark.
	type block struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	text := missingToolsAnswer
	if reply.IsError {
		text = missingToolsNoNeed
	}
	written, err := json.Marshal(struct {
		Content    []block `json:"content"`
		IsError    bool    `json:"isError,omitempty"`
		ResultType string  `json:"resultType"`
	}{Content: []block{{Type: "text", Text: text}}, IsError: reply.IsError, ResultType: "complete"})
	complete := new(mcp.CallToolResult)
	if err != nil || json.Unmarshal(written, complete) != nil {
		return reply
	}
	return complete
}

// discover reads the server's whole tool listing itself, for a call that comes before the
// wrapper has seen one: on a new process, or on a server built for one request, from an agent
// that was listed the tools elsewhere. It asks the rest of the middleware chain, as the call's
// own connection, in a request of its own. One listing is read at a time, and a call waits for
// it until a second after it began at most: past that it goes on as it came, and the listing
// ends by itself. A listing that fails is not tried again for a minute.
func (w *instrumented) discover(ctx context.Context, next mcp.MethodHandler, session *mcp.ServerSession, extra *mcp.RequestExtra, meta mcp.Meta) {
	w.mu.Lock()
	if w.catalog.whole || (!w.catalog.failed.IsZero() && time.Since(w.catalog.failed) < listPause) {
		w.mu.Unlock()
		return
	}
	done := w.catalog.reading
	if done == nil {
		done = make(chan struct{})
		w.catalog.reading, w.catalog.started = done, time.Now()
		// A request of its own, with copies of what says who is asking: what the server's
		// middleware does to them, the call never sees.
		var own *mcp.RequestExtra
		if extra != nil {
			own = &mcp.RequestExtra{Header: extra.Header.Clone()}
			if token := extra.TokenInfo; token != nil {
				copied := *token
				copied.Scopes = slices.Clone(token.Scopes)
				copied.Extra, _ = plainCopy(token.Extra).(map[string]any)
				own.TokenInfo = &copied
			}
		}
		asked, _ := plainCopy(map[string]any(meta)).(map[string]any)
		delete(asked, "progressToken")
		// The call's values, without its end: the listing may outlive a call that stops waiting.
		go w.list(context.WithoutCancel(ctx), next, session, own, asked, w.catalog.generation, done)
	}
	left := listWait - time.Since(w.catalog.started)
	w.mu.Unlock()
	if left <= 0 {
		return
	}
	wait := time.NewTimer(left)
	defer wait.Stop()
	select {
	case <-done:
	case <-wait.C:
	case <-ctx.Done():
	}
}

// plainCopy copies the maps and lists of a JSON value, so that changing the copy changes nothing
// in the value. Anything else in it is shared.
func plainCopy(value any) any {
	switch v := value.(type) {
	case map[string]any:
		if v == nil {
			return v
		}
		out := make(map[string]any, len(v))
		for key, item := range v {
			out[key] = plainCopy(item)
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = plainCopy(item)
		}
		return out
	}
	return value
}

// list reads the whole listing, page by page, into the catalog: all of it, or nothing.
func (w *instrumented) list(ctx context.Context, next mcp.MethodHandler, session *mcp.ServerSession, extra *mcp.RequestExtra, meta map[string]any, since int, done chan struct{}) {
	var all []*mcp.Tool
	whole := false
	defer func() {
		// Whatever the server's middleware does, a panic included.
		_ = recover()
		if whole {
			func() {
				defer func() { _ = recover() }()
				w.describe(all)
				w.note(all, true, since)
			}()
		}
		w.mu.Lock()
		if !whole {
			w.catalog.failed = time.Now()
		}
		w.catalog.reading = nil
		w.mu.Unlock()
		close(done)
	}()
	ctx, cancel := context.WithTimeout(ctx, listTimeout)
	defer cancel()
	cursor, seen := "", map[string]bool{}
	for range listPages {
		asked, _ := plainCopy(meta).(map[string]any)
		result, err := next(ctx, "tools/list", &mcp.ListToolsRequest{Session: session, Params: &mcp.ListToolsParams{Meta: asked, Cursor: cursor}, Extra: extra})
		page, ok := result.(*mcp.ListToolsResult)
		// Also a listing that comes after its time: it may be of a server that has moved on.
		if err != nil || !ok || page == nil || ctx.Err() != nil || len(all)+len(page.Tools) > maxTools {
			return
		}
		all = append(all, page.Tools...)
		if page.NextCursor == "" {
			whole = true
			return
		}
		// A cursor seen before: the listing goes round.
		if seen[page.NextCursor] {
			return
		}
		seen[page.NextCursor] = true
		cursor = page.NextCursor
	}
}

// withArguments is a copy of the request with other arguments: the request and its parameters
// are new, and everything else in them is the request's own.
func withArguments(request *mcp.CallToolRequest, arguments []byte) *mcp.CallToolRequest {
	params := *request.Params
	params.Arguments = arguments
	copied := *request
	copied.Params = &params
	return &copied
}
