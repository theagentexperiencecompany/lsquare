// An MCP server with one tool, recorded by LSquare, served over HTTP on your machine.
//
// See its first event without sending anything:
//
//	LSQUARE_DRY_RUN=1 go run ./examples/server
//
// and call the tool from another terminal:
//
//	curl -s http://localhost:3000/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_forecast","arguments":{"city":"Paris"}}}'
//
// With LSQUARE_KEY set instead of LSQUARE_DRY_RUN, the event is sent. PORT changes the port.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	lsquare "github.com/theagentexperiencecompany/lsquare/packages/go"
	"github.com/theagentexperiencecompany/lsquare/packages/go/lsquaremcp"
)

type forecastInput struct {
	City string `json:"city" jsonschema:"the city to forecast"`
}

var (
	info     = &mcp.Implementation{Name: "weather", Version: "1.0.0"}
	forecast = &mcp.Tool{Name: "get_forecast", Description: "Gives tomorrow's forecast for a city."}
)

func getForecast(ctx context.Context, request *mcp.CallToolRequest, input forecastInput) (*mcp.CallToolResult, any, error) {
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "Tomorrow in " + input.City + ": sunny, 21°C."}}}, nil, nil
}

func main() {
	// Once, for the whole process. Without a key, and outside a dry run, it records nothing.
	client := lsquare.New(lsquare.Options{APIKey: os.Getenv("LSQUARE_KEY")})

	server := mcp.NewServer(info, nil)
	mcp.AddTool(server, forecast, getForecast)

	// The one line per server. Tools and ServerInfo are what the server was made with: with them,
	// a call carries its tool's description and the server's name from the first one.
	lsquaremcp.Instrument(server, client, &lsquaremcp.Options{Tools: []*mcp.Tool{forecast}, ServerInfo: info})

	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, &mcp.StreamableHTTPOptions{Stateless: true})
	mux := http.NewServeMux()
	mux.Handle("/mcp", handler)
	port := os.Getenv("PORT")
	if port == "" {
		port = "3000"
	}
	web := &http.Server{Addr: "localhost:" + port, Handler: mux, ReadHeaderTimeout: 10 * time.Second}

	interrupted, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	// Closed once the calls still running at the interrupt have replied, or 10 seconds on.
	drained := make(chan struct{})
	go func() {
		defer close(drained)
		<-interrupted.Done()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = web.Shutdown(ctx)
	}()
	log.Printf("Listening on http://%s/mcp", web.Addr)
	if err := web.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Print(err)
		stop()
	}
	// ListenAndServe returns as soon as the listener closes: the calls in flight are waited for
	// here, so their events are recorded before the client shuts down.
	<-drained

	// Before the process exits: what is queued is sent, for 5 seconds at most.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client.Shutdown(ctx)
}
