package diagnostics

import (
	"bytes"
	"encoding/json"
	"errors"
)

// PlatformView is the strict local representation of a cloud EventView. The
// message is always regenerated from the local catalog instead of trusting
// arbitrary server text.
type PlatformView struct {
	Event
	Message string `json:"message"`
}

type platformWire struct {
	Event
	Message string `json:"message,omitempty"`
}

// SafePlatformViews drops malformed or non-contract event objects. Unknown
// fields are rejected so the local browser never receives unexpected values.
func SafePlatformViews(items []map[string]any) []PlatformView {
	out := make([]PlatformView, 0, len(items))
	for _, item := range items {
		raw, err := json.Marshal(item)
		if err != nil {
			continue
		}
		var wire platformWire
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&wire); err != nil {
			continue
		}
		event, err := normalizePlatformEvent(wire.Event)
		if err != nil {
			continue
		}
		out = append(out, PlatformView{
			Event:   event,
			Message: Message(event.MessageCode),
		})
	}
	return out
}

func normalizePlatformEvent(event Event) (Event, error) {
	if event.Component != ComponentPlatform {
		return Event{}, errors.New("unexpected platform component")
	}
	if event.EventCode != "platform_request_succeeded" && event.EventCode != "platform_request_failed" {
		return Event{}, errors.New("unexpected platform event code")
	}
	toolName := event.ToolName
	event.ToolName = ""
	if err := Validate(event, nil); err != nil {
		return Event{}, err
	}
	if toolName != "" {
		if safeToolName(toolName) == "" {
			return Event{}, errors.New("invalid tool_name")
		}
		event.ToolName = toolName
	}
	return event, nil
}
