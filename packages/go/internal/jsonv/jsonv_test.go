package jsonv

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

func TestDecodeReadsObjectsInJavaScriptsOrder(t *testing.T) {
	for in, want := range map[string]string{
		`{"b":1,"10":2,"2":3,"a":4}`:                         `{"2":3,"10":2,"b":1,"a":4}`,
		`{"memo":"x","1":{"z":1,"0":2},"0":[{"5":1,"a":2}]}`: `{"0":[{"5":1,"a":2}],"1":{"0":2,"z":1},"memo":"x"}`,
		// Not array indices: a leading zero, a sign, a fraction, 2^32 - 1 and above.
		`{"x":0,"01":1,"-1":2,"1.5":3,"4294967295":4,"4294967294":5,"":6}`: `{"4294967294":5,"x":0,"01":1,"-1":2,"1.5":3,"4294967295":4,"":6}`,
		// A key given twice keeps its first place and its last value.
		`{"a":1,"b":2,"a":3}`: `{"a":3,"b":2}`,
	} {
		value, err := Decode([]byte(in))
		if err != nil {
			t.Fatal(err)
		}
		if got := string(Marshal(value)); got != want {
			t.Errorf("%s is read as %s, want %s", in, got, want)
		}
	}
}

// A value nested without end must not use the stack up: past MaxDepth it is read past, and left empty.
func TestDecodeStopsBuildingPastMaxDepth(t *testing.T) {
	for _, pair := range []string{"[]", "{}"} {
		open, shut := pair[:1], pair[1:]
		if open == "{" {
			open = `{"a":`
		}
		const depth = 200_000
		deep := strings.Repeat(open, depth) + "1" + strings.Repeat(shut, depth)
		value, err := Decode([]byte(`{"before":1,"deep":` + deep + `,"after":"kept"}`))
		// encoding/json may refuse it itself; what matters is that this returns.
		if err != nil {
			continue
		}
		object := value.(*Object)
		if after, _ := object.Get("after"); after != "kept" {
			t.Errorf("what follows the deep value is %v", after)
		}
		levels := 0
		for item, _ := object.Get("deep"); ; levels++ {
			switch v := item.(type) {
			case []Value:
				if len(v) == 0 {
					item = nil
				} else {
					item = v[0]
				}
				continue
			case *Object:
				item, _ = v.Get("a")
				continue
			}
			break
		}
		if levels > MaxDepth+1 {
			t.Errorf("built %d levels", levels)
		}
	}
	if _, err := Decode([]byte(`[1,2`)); err == nil {
		t.Error("a list that doesn't close was read")
	}
	if _, err := Decode([]byte(`{"a":1} x`)); err == nil {
		t.Error("text after the value was read")
	}
}

func TestDeleteLetsGoOfWhatItHeld(t *testing.T) {
	object := NewObject()
	for _, key := range []string{"a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"} {
		object.Set(key, key)
	}
	object.Delete("m")
	object.Delete("a")
	if got := string(Marshal(object)); got != `{"b":"b","c":"c","d":"d","e":"e","f":"f","g":"g","h":"h","i":"i","j":"j","k":"k","l":"l"}` {
		t.Errorf("after deleting: %s", got)
	}
	if spare := object.vals[:cap(object.vals)]; spare[len(object.vals)] != nil {
		t.Errorf("the deleted value is still held: %v", spare[len(object.vals)])
	}
	if value, ok := object.Get("l"); !ok || value != "l" || object.Has("m") || object.Has("a") {
		t.Error("lookups are wrong after a delete")
	}
	object.Set("n", 1)
	if value, _ := object.Get("n"); value != 1 {
		t.Error("a key set after a delete isn't found")
	}
}

func TestStringsAreWrittenAsJSONStringifyWritesThem(t *testing.T) {
	in := "a\"b\\c/\b\f\n\r\t\x00\x1f\x7f<>&é界😀  "
	want := `"a\"b\\c/\b\f\n\r\t\u0000\u001f` + "\x7f<>&é界😀  " + `"`
	if got := Quote(in); got != want {
		t.Errorf("got %s, want %s", got, want)
	}
	// Each invalid byte is U+FFFD, as encoding/json writes it: not one for the run.
	if got := ValidString("a\xff\xfeb"); got != "a��b" || Quote("a\xff\xfeb") != `"a`+"��"+`b"` {
		t.Errorf("got %q", got)
	}
}

// An integer JavaScript can't hold exactly is written as JavaScript reads it: the ingestion
// measures an event as it writes it back, and the exact digits could be one byte shorter.
func TestIntegersAreWrittenAsJavaScriptReadsThem(t *testing.T) {
	for n, want := range map[int64]string{
		0: "0", -7: "-7", 1<<53 - 1: "9007199254740991", -(1<<53 - 1): "-9007199254740991",
		1 << 53: "9007199254740992", 1<<53 + 1: "9007199254740992", 99_999_999_999_999_999: "100000000000000000",
		math.MaxInt64: "9223372036854776000", math.MinInt64: "-9223372036854776000",
	} {
		if got := string(Int(n)); got != want {
			t.Errorf("%d is written %s, want %s", n, got, want)
		}
	}
}

type named string

func (named) MarshalJSON() ([]byte, error) { panic("never called") }

// A plain value is read by its kind, without any method of its own.
func TestPlainValuesAreReadByKind(t *testing.T) {
	text := "x"
	for _, test := range []struct {
		in   any
		want string
	}{
		{nil, "null"}, {"a\xffb", `"a�b"`}, {true, "true"}, {int8(-3), "-3"}, {uint64(math.MaxUint64), "18446744073709552000"},
		{float32(0.1), "0.1"}, {1.5, "1.5"}, {math.Inf(1), ""}, {json.Number("12.50"), "12.5"}, {json.Number("1e400"), ""},
		{json.Number("0x10"), ""}, {json.Number(""), ""}, {named("pro"), `"pro"`}, {&text, `"x"`}, {(*int)(nil), "null"},
		{[]string{"x"}, ""}, {map[string]any{}, ""}, {struct{}{}, ""}, {func() {}, ""},
	} {
		got := PlainValue(test.in)
		if _, not := got.(NotJSON); not {
			if test.want != "" {
				t.Errorf("%#v isn't a plain value, want %s", test.in, test.want)
			}
			continue
		}
		if written := string(Marshal(got)); written != test.want {
			t.Errorf("%#v is %s, want %s", test.in, written, test.want)
		}
	}
}
