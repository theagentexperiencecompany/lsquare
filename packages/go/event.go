package lsquare

import (
	"encoding/json"
	"maps"

	"github.com/theagentexperiencecompany/lsquare/packages/go/internal/jsonv"
)

// How an event, which the SDK holds as ordered JSON, is given to BeforeSend and OnError as an
// Event, and how what BeforeSend returns is read back.

func textOf(object *jsonv.Object, key string) string {
	value, _ := object.Get(key)
	text, _ := value.(string)
	return text
}

func textPointer(object *jsonv.Object, key string) *string {
	value, _ := object.Get(key)
	if text, ok := value.(string); ok {
		return &text
	}
	return nil
}

func namedOf(object *jsonv.Object, key string) *Named {
	value, _ := object.Get(key)
	named, ok := value.(*jsonv.Object)
	if !ok || named == nil {
		return nil
	}
	return &Named{Name: textOf(named, "name"), Version: textOf(named, "version")}
}

// goValue is a JSON value as encoding/json would decode it: for metadata, whose values are plain.
func goValue(value jsonv.Value) any {
	switch v := value.(type) {
	case jsonv.Num:
		return v.Float64()
	case string, bool, nil:
		return v
	}
	return nil
}

// publicEvent is the event as an Event. limited: Identify failed, so it is sent as limited
// diagnostics, though it isn't reduced to them until BeforeSend has returned.
func publicEvent(event *jsonv.Object, limited bool) *Event {
	out := &Event{
		EventID:            textOf(event, "eventId"),
		Kind:               textOf(event, "kind"),
		CapturedAt:         textOf(event, "capturedAt"),
		CallID:             textOf(event, "callId"),
		ToolName:           textOf(event, "toolName"),
		OrgID:              textOf(event, "orgId"),
		UserID:             textOf(event, "userId"),
		ConversationID:     textOf(event, "conversationId"),
		Status:             textOf(event, "status"),
		LimitedDiagnostics: limited || textOf(event, "identity") == "failed",
		Round:              textOf(event, "round"),
		SessionID:          textOf(event, "sessionId"),
		SessionSource:      textOf(event, "sessionSource"),
		Extra:              textOf(event, "extra"),
		Intent:             textOf(event, "intent"),
		ToolDescription:    textOf(event, "toolDescription"),
		ServerInstructions: textOf(event, "serverInstructions"),
		Client:             namedOf(event, "client"),
		Server:             namedOf(event, "server"),
		ProtocolVersion:    textOf(event, "protocolVersion"),
	}
	if out.Kind == kindTurn {
		// A turn's client and error are the turn's to say: in Turn, where BeforeSend changes them.
		defer func() { out.Turn, out.Client, out.Error = publicTurn(event), nil, nil }()
	} else if value, _ := event.Get("agent"); value != nil {
		if agent, ok := value.(*jsonv.Object); ok {
			out.Agent = publicTurn(agent)
		}
	}
	out.TurnRef = textOf(event, "turnRef")
	if duration, ok := event.Get("durationMs"); ok {
		if number, isNumber := duration.(jsonv.Num); isNumber {
			out.DurationMs = number.Float64()
		}
	}
	if input, ok := event.Get("input"); ok {
		out.Input = json.RawMessage(jsonv.Marshal(input))
	}
	if value, _ := event.Get("result"); value != nil {
		if result, ok := value.(*jsonv.Object); ok {
			out.Result = &Result{}
			if content, ok := result.Get("content"); ok {
				list, _ := content.([]any)
				out.Result.Content = make([]Block, 0, len(list))
				for _, item := range list {
					block, _ := item.(*jsonv.Object)
					public := Block{Type: textOf(block, "type"), Text: textPointer(block, "text"), URI: textPointer(block, "uri"), Name: textPointer(block, "name"), MIMEType: textPointer(block, "mimeType")}
					if bytes, ok := block.Get("bytes"); ok {
						if number, isNumber := bytes.(jsonv.Num); isNumber {
							size := int64(number.Float64())
							public.Bytes = &size
						}
					}
					out.Result.Content = append(out.Result.Content, public)
				}
			}
			if structured, ok := result.Get("structuredContent"); ok {
				out.Result.StructuredContent = json.RawMessage(jsonv.Marshal(structured))
			}
			flag, _ := result.Get("isError")
			out.Result.IsError = flag == true
		}
	}
	if value, _ := event.Get("error"); value != nil {
		if failure, ok := value.(*jsonv.Object); ok {
			out.Error = &CallError{Message: textOf(failure, "message"), Type: textOf(failure, "type"), Category: textOf(failure, "category")}
			if message, has := failure.Get("message"); has && message == "" {
				out.Error.emptyMessage = true
			}
			switch code, _ := failure.Get("code"); c := code.(type) {
			case jsonv.Num:
				out.Error.Code = c.Float64()
			case string:
				out.Error.Code = c
			}
		}
	}
	if value, _ := event.Get("metadata"); value != nil {
		if metadata, ok := value.(*jsonv.Object); ok {
			out.metadataOrder = metadata.Keys()
			out.Metadata = make(map[string]any, metadata.Len())
			for i := 0; i < metadata.Len(); i++ {
				key, item := metadata.At(i)
				out.Metadata[key] = goValue(item)
			}
		}
	}
	if value, _ := event.Get("redactions"); value != nil {
		if counts, ok := value.(*jsonv.Object); ok {
			out.Redactions = make(map[Pattern]int, counts.Len())
			for i := 0; i < counts.Len(); i++ {
				key, item := counts.At(i)
				if number, isNumber := item.(jsonv.Num); isNumber {
					out.Redactions[Pattern(key)] = int(number.Float64())
				}
			}
		}
	}
	if value, _ := event.Get("dropped"); value != nil {
		if dropped, ok := value.(*jsonv.Object); ok {
			out.Dropped = make(map[string]string, dropped.Len())
			for i := 0; i < dropped.Len(); i++ {
				key, item := dropped.At(i)
				out.Dropped[key], _ = item.(string)
			}
		}
	}
	if list, ok := func() ([]any, bool) { value, _ := event.Get("truncated"); l, ok := value.([]any); return l, ok }(); ok {
		for _, item := range list {
			if name, isText := item.(string); isText {
				out.Truncated = append(out.Truncated, name)
			}
		}
	}
	return out
}

// rawValue reads JSON a hook gave. What isn't JSON is left out of the event, and noted in dropped.
func rawValue(raw json.RawMessage) jsonv.Value {
	value, err := jsonv.Decode(raw)
	if err != nil {
		return jsonv.NotJSON{}
	}
	return value
}

func namedObject(named *Named) *jsonv.Object {
	out := jsonv.NewObject()
	out.Set("name", jsonv.ValidString(named.Name))
	if named.Version != "" {
		out.Set("version", jsonv.ValidString(named.Version))
	}
	return out
}

// hookOutput is what BeforeSend returned, as the fields the SDK reads back from it: everything
// else of the event is restored from before the hook.
func hookOutput(event *Event) *jsonv.Object {
	out := jsonv.NewObject()
	if event.Client != nil {
		out.Set("client", namedObject(event.Client))
	}
	if event.Server != nil {
		out.Set("server", namedObject(event.Server))
	}
	if event.ProtocolVersion != "" {
		out.Set("protocolVersion", jsonv.ValidString(event.ProtocolVersion))
	}
	if event.Intent != "" {
		out.Set("intent", jsonv.ValidString(event.Intent))
	}
	if event.ToolDescription != "" {
		out.Set("toolDescription", jsonv.ValidString(event.ToolDescription))
	}
	if event.ServerInstructions != "" {
		out.Set("serverInstructions", jsonv.ValidString(event.ServerInstructions))
	}
	if event.Input != nil {
		out.Set("input", rawValue(event.Input))
	}
	if event.Result != nil {
		result := jsonv.NewObject()
		if event.Result.Content != nil {
			blocks := make([]any, len(event.Result.Content))
			for i, block := range event.Result.Content {
				item := jsonv.NewObject()
				item.Set("type", jsonv.ValidString(block.Type))
				for _, field := range []struct {
					key  string
					text *string
				}{{"uri", block.URI}, {"name", block.Name}, {"mimeType", block.MIMEType}, {"text", block.Text}} {
					if field.text != nil {
						item.Set(field.key, jsonv.ValidString(*field.text))
					}
				}
				if block.Bytes != nil {
					item.Set("bytes", jsonv.Int(*block.Bytes))
				}
				blocks[i] = item
			}
			result.Set("content", blocks)
		}
		if event.Result.StructuredContent != nil {
			result.Set("structuredContent", rawValue(event.Result.StructuredContent))
		}
		if event.Result.IsError {
			result.Set("isError", true)
		}
		out.Set("result", result)
	}
	if event.Error != nil {
		failure := jsonv.NewObject()
		if event.Error.Message != "" || event.Error.emptyMessage {
			failure.Set("message", jsonv.ValidString(event.Error.Message))
		}
		if event.Error.Type != "" {
			failure.Set("type", jsonv.ValidString(event.Error.Type))
		}
		if event.Error.Code != nil {
			failure.Set("code", jsonv.PlainValue(event.Error.Code))
		}
		out.Set("error", failure)
	}
	if event.Agent != nil {
		// Its turn ID is the event's, as it was.
		agent := jsonv.NewObject()
		turnContent(agent, event.Agent)
		out.Set("agent", agent)
	}
	if event.Metadata != nil {
		out.Set("metadata", metadataObject(event.Metadata, event.metadataOrder))
	}
	return out
}

// metadataObject is metadata a hook returned, in the order it had before the hook, so a hook that
// changes nothing changes no number: the keys it had first, then the others, sorted, and those
// that are array indices before all, as JavaScript has them.
func metadataObject(values map[string]any, order []string) *jsonv.Object {
	out := jsonv.NewObject()
	rest := maps.Clone(values)
	for _, key := range order {
		if value, kept := rest[key]; kept {
			out.Set(jsonv.ValidString(key), jsonv.PlainValue(value))
			delete(rest, key)
		}
	}
	added := jsonv.ObjectFromMap(rest)
	for i := 0; i < added.Len(); i++ {
		key, value := added.At(i)
		out.Set(key, value)
	}
	out.IndicesFirst()
	return out
}
