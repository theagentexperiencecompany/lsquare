//! Generated inputs, written by this SDK and by the TypeScript SDK, compared byte for byte.
//!
//! The shared cases say what must hold; this looks for what they don't cover. The inputs are made
//! around what a port gets wrong: text of more than one byte a character next to every bounded
//! read (a key's last 256 UTF-16 units, a quote looked for 256 on, a URL password's 256, a
//! `data:` header's 1,024), each key format at and just under its shortest length, numbers as
//! JavaScript reads them, and objects with more entries than are kept.
//!
//! `LSQUARE_GENERATED_CASES` sets how many are made (1,500 by default) and
//! `LSQUARE_GENERATED_SEED` from what. A case that differs is printed with both events: what is
//! in it is made up here, none of it anyone's.

use std::fmt::Write as _;
use std::path::PathBuf;

use serde_json::Value;

use crate::cases_test::{build, contract_dir, file_url, node, sdk, skip_without_node, TYPESCRIPT};
use crate::json::stringify;
use crate::testing::{parse, Js};

/// xorshift64*: the same inputs on every machine, for a seed.
struct Random(u64);

impl Random {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, limit: usize) -> usize {
        (self.next() % limit as u64) as usize
    }

    fn within(&mut self, low: usize, high: usize) -> usize {
        low + self.below(high - low + 1)
    }

    fn chance(&mut self, percent: u64) -> bool {
        self.next() % 100 < percent
    }

    fn pick<'a>(&mut self, items: &[&'a str]) -> &'a str {
        items[self.below(items.len())]
    }

    fn from(&mut self, alphabet: &str, length: usize) -> String {
        let letters: Vec<char> = alphabet.chars().collect();
        (0..length)
            .map(|_| letters[self.below(letters.len())])
            .collect()
    }
}

const ALNUM: &str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
const UPPER: &str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

/// One character each, of one, two, three and four bytes; the last is two UTF-16 units.
const FILLERS: [&str; 12] = [
    "a", "x", "7", " ", "é", "ß", "漢", "か", "한", "😀", "𝒳", "\u{301}",
];

/// Where one thing ends and the next begins.
const BETWEEN: [&str; 22] = [
    " ",
    "",
    "\n",
    "\\n",
    ", ",
    ": ",
    "=",
    "\"",
    "'",
    "\\\"",
    "/",
    ".",
    "-",
    "_",
    "\t",
    "\u{a0}",
    "\u{2028}",
    "\u{3000}",
    "\\u0041",
    "\\x41",
    "\\U00000041",
    "@",
];

/// Key names that hold a secret, ones that only look it, and plain ones.
const KEYS: [&str; 30] = [
    "password",
    "passwd",
    "dbPassword",
    "client_secret",
    "X-Api-Key",
    "AWS_SECRET_ACCESS_KEY",
    "apiKey",
    "api_key",
    "token",
    "accessToken",
    "auth",
    "Authorization",
    "secret",
    "passphrase",
    "private_key",
    "credentials",
    "session",
    "cookie",
    "apiKeyId",
    "maxTokens",
    "author",
    "name",
    "note",
    "id",
    "0",
    "7",
    "42",
    "007",
    "4294967295",
    "-1",
];

const PLAIN: [&str; 14] = [
    "lorem ipsum",
    "the order shipped",
    "see https://example.com/docs?page=2#top",
    "ann@example.com",
    "a.b+tag@mail.example.co.uk",
    "+1 (415) 555-0100",
    "+44 20 7946 0958",
    "4111 1111 1111 1111",
    "5500-0000-0000-0004",
    "378282246310005",
    "4111111111111112",
    "192.168.1.10",
    "2001:db8::8a2e:370:7334",
    "[EMAIL_1] and [SECRET_3]",
];

const ADDRESSES: [&str; 8] = [
    "10.0.0.1:8080",
    "::1",
    "fe80::1%eth0",
    "::ffff:192.0.2.1",
    "[2001:db8::1]:443",
    "1.2.3.4.5",
    "256.1.1.1",
    "2001:db8:0:0:0:0:2:1",
];

const NUMBERS: [&str; 38] = [
    "0",
    "-0",
    "1",
    "-17",
    "1.0",
    "0.1",
    "1.5",
    "1e21",
    "1e-7",
    "123456.789e3",
    "5e-324",
    "-1.5e300",
    "100000000000000000000",
    "0.000001",
    "4111111111111111",
    "4111111111111111.0",
    "378282246310005",
    "14155550100",
    "2.5e-5",
    "1E5",
    // Past 2^53, where JavaScript holds the nearest double.
    "9007199254740993",
    "-9007199254740993",
    "123456789012345678",
    "999999999999999999",
    "18446744073709551615",
    "-9223372036854775808",
    "123456789012345678901234567890",
    // Where a parser that isn't exact reads another double than JavaScript does.
    "9007199254740993.0",
    "100000000000000008193",
    "4000000000000150.0",
    "4111111111111111.5",
    "2.2250738585072011e-308",
    "1.7976931348623157e308",
    "0.1e1",
    "8.41e21",
    "4.35",
    "0.000001234567890123456789",
    "123456789.123456789e-5",
];

/// A run of one character, most often short, sometimes as long as a bounded read or a limit.
fn run(random: &mut Random) -> String {
    let filler = random.pick(&FILLERS);
    let length = match random.below(100) {
        0..=59 => random.below(6),
        60..=69 => random.within(84, 96),
        70..=81 => random.within(124, 131),
        82..=91 => random.within(250, 260),
        92..=95 => random.within(506, 518),
        96..=98 => random.within(1_018, 1_030),
        _ => random.within(3_990, 4_010),
    };
    filler.repeat(length)
}

/// A run of one character, a few more or fewer than `length`: either side of a bounded read.
fn near(random: &mut Random, length: usize) -> String {
    let filler = random.pick(&FILLERS);
    filler.repeat(random.within(length - 4, length + 4))
}

/// A made-up key of a known format: at its shortest length, one short of it, or longer.
fn key(random: &mut Random) -> String {
    let (prefix, alphabet, least): (&str, &str, usize) = match random.below(24) {
        0 => ("lsq_", ALNUM, 40),
        1 => ("sk-", ALNUM, 20),
        2 => ("sk_live_", ALNUM, 16),
        3 => ("rk_test_", ALNUM, 16),
        4 => ("whsec_", ALNUM, 24),
        5 => ("ghp_", ALNUM, 36),
        6 => ("gho_", ALNUM, 36),
        7 => ("ghs_", ALNUM, 36),
        8 => ("ghu_", ALNUM, 36),
        9 => ("ghr_", ALNUM, 36),
        10 => ("github_pat_", ALNUM, 20),
        11 => ("glpat-", ALNUM, 20),
        12 => ("npm_", ALNUM, 36),
        13 => ("xoxb-", ALNUM, 10),
        14 => ("xapp-", ALNUM, 10),
        15 => ("hooks.slack.com/services/", ALNUM, 20),
        16 => ("AKIA", UPPER, 16),
        17 => ("ASIA", UPPER, 16),
        18 => ("AIza", ALNUM, 35),
        19 => ("ya29.", ALNUM, 20),
        20 => ("GOCSPX-", ALNUM, 20),
        21 => ("phx_", ALNUM, 20),
        22 => ("aws_secret_access_key = ", ALNUM, 40),
        _ => ("hooks.slack-gov.com\\/triggers\\/", ALNUM, 20),
    };
    let length = match random.below(4) {
        0 => least - 1,
        1 => least,
        2 => least + 1,
        _ => least + random.below(30),
    };
    format!("{prefix}{}", random.from(alphabet, length))
}

/// A made-up token of another kind: a JWT, a Bearer or Basic header, a PEM block.
fn token(random: &mut Random) -> String {
    match random.below(8) {
        0 => format!(
            "eyJ{}.{}.{}",
            random.from(ALNUM, 12),
            random.from(ALNUM, 12),
            random.from(ALNUM, 12)
        ),
        1 => format!(
            "eyJ{}.{}.{}",
            random.from(ALNUM, 9),
            random.from(ALNUM, 12),
            random.from(ALNUM, 12)
        ),
        2 => format!("Bearer {}7", random.from(ALNUM, 19)),
        3 => format!(
            "bearer\u{a0}{}",
            random.from("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 24)
        ),
        4 => format!("Basic {}==", random.from(ALNUM, 22)),
        5 => format!(
            "-----BEGIN RSA PRIVATE KEY-----\n{}\n-----END RSA PRIVATE KEY-----",
            random.from(ALNUM, 48)
        ),
        6 => format!("-----BEGIN PRIVATE KEY-----\n{}", random.from(ALNUM, 48)),
        _ => key(random),
    }
}

/// A URL with credentials, its password as long as its bounded read or not.
fn url(random: &mut Random) -> String {
    let scheme = random.pick(&["https://", "postgres://", "https:\\/\\/", "redis://"]);
    let user = match random.below(3) {
        0 => String::new(),
        1 => "user".to_owned(),
        _ => run(random),
    };
    let password = match random.below(3) {
        0 => random.from(ALNUM, 12),
        1 => run(random),
        _ => format!("{}{}", run(random), random.from(ALNUM, 4)),
    };
    format!("{scheme}{user}:{password}@db.example.com/path")
}

/// A `data:` URL, its header as long as its bounded read or not.
fn data_url(random: &mut Random) -> String {
    format!(
        "data:image/png;name={};base64,{}",
        run(random),
        "QUJDRA==".repeat(random.within(1, 40))
    )
}

/// Text that holds a key and its value, as JSON, Python, a header, a query or a config file does.
fn keyed(random: &mut Random) -> String {
    let name = match random.below(6) {
        // The name's own length next to the scanner's reads.
        0 => format!("pass{}word", run(random)),
        1 => format!("{}password", run(random)),
        2 => format!("api{}Key", run(random)),
        _ => random.pick(&KEYS).to_owned(),
    };
    let value = match random.below(7) {
        0 => token(random),
        1 => run(random),
        2 => format!("{}{}", run(random), random.from(ALNUM, 6)),
        3 => random.pick(&PLAIN).to_owned(),
        4 => format!("hunter2{}", random.pick(&BETWEEN)),
        5 => random.pick(&NUMBERS).to_owned(),
        _ => format!("{} {}", random.from(ALNUM, 8), run(random)),
    };
    match random.below(22) {
        // A Python string with a quote in it: one that can end it is looked for 256 units on.
        16 => format!(
            "{{'{name}': 'it's {}'{}",
            near(random, 250),
            random.pick(&[", 'page': 2}", "}", " and more", ""])
        ),
        17 => format!(
            "{name}='{}\"{}'{}",
            random.from(ALNUM, 3),
            near(random, 253),
            random.pick(&[", retries=3)", ")", " ", ""])
        ),
        // What pydantic prints of a value it refused: its end is looked for 1,024 units on.
        18 => format!(
            "1 validation error for Settings\n{name}\n  Input should be a valid string [type=string_type, input_value={}, input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type",
            near(random, 1_024)
        ),
        19 => format!(
            "{name}\n  Input should be a valid integer [type=int_parsing, input_value='{}{}', input_type=str]",
            random.from(ALNUM, 4),
            near(random, 1_018)
        ),
        20 => format!(
            "{name}\n  Field required [type=missing, input_value={{'{name}': '{value}', 'user': 'a'}}, input_type=dict]"
        ),
        21 => format!(
            "[type=missing, input_value={}{}\\n  next line, input_type=str]",
            near(random, 1_020),
            random.pick(&["", "'", " "])
        ),
        0 => format!("{{\"{name}\": \"{value}\"}}"),
        1 => format!("{{\"{name}\":\"{value}\",\"other\":1}}"),
        2 => format!("{{\\\"{name}\\\": \\\"{value}\\\"}}"),
        3 => format!("{{'{name}': '{value}'}}"),
        4 => format!("{name}='{value}'"),
        5 => format!("{name}={value}"),
        6 => format!("{name}: {value}"),
        7 => format!("\"{name}\" => \"{value}\""),
        8 => format!("Settings({name}='{value}', retries=3)"),
        9 => format!("{name}=SecretStr('{value}')"),
        10 => format!("https://example.com/cb?{name}={value}&page=2"),
        11 => format!("export {name}=\"{value}\"\n"),
        12 => format!("{name}:\n  {value}\n"),
        13 => format!("{{\"{name}\": {{\"inner\": \"{value}\"}}}}"),
        14 => format!("{{\"{name}\": [\"{value}\", 2]}}"),
        _ => format!("{{\"{name}\" : `{value}`}}"),
    }
}

fn piece(random: &mut Random) -> String {
    match random.below(12) {
        0 | 1 => random.pick(&PLAIN).to_owned(),
        2 => random.pick(&ADDRESSES).to_owned(),
        3 => key(random),
        4 => token(random),
        5 => url(random),
        6 => data_url(random),
        7 | 8 => keyed(random),
        9 | 10 => run(random),
        _ => random.pick(&NUMBERS).to_owned(),
    }
}

fn text(random: &mut Random) -> String {
    let mut out = String::new();
    for _ in 0..random.within(1, 5) {
        out.push_str(&piece(random));
        out.push_str(random.pick(&BETWEEN));
    }
    if random.chance(50) {
        out.push_str(&piece(random));
    }
    out
}

/// A value, as JSON text.
fn value(random: &mut Random, depth: usize, out: &mut String) {
    let leaf = depth >= 12 || (depth > 0 && random.chance(55));
    if leaf {
        match random.below(10) {
            0 => out.push_str(random.pick(&NUMBERS)),
            1 => out.push_str(random.pick(&["true", "false", "null"])),
            _ => out.push_str(&stringify(&Value::String(text(random)))),
        }
        return;
    }
    // Deep, now and then: past the levels that are kept.
    let deeper = if random.chance(3) {
        depth + 1
    } else {
        depth + 3
    };
    if random.chance(30) {
        out.push('[');
        let length = if random.chance(3) {
            random.within(99, 104)
        } else {
            random.below(4)
        };
        for index in 0..length {
            if index > 0 {
                out.push(',');
            }
            value(random, deeper.max(if length > 20 { 12 } else { 0 }), out);
        }
        out.push(']');
        return;
    }
    out.push('{');
    let many = random.chance(3);
    let length = if many {
        random.within(99, 104)
    } else {
        random.within(1, 4)
    };
    // Of the wide ones, some have only indexes for keys, the highest first: more of them than
    // are kept, and none in the order they are read in.
    let indexes_only = many && random.chance(40);
    let mut used: Vec<String> = Vec::new();
    for index in 0..length {
        let name = match random.below(10) {
            _ if indexes_only => (7 * (length - index)).to_string(),
            _ if many && index % 3 != 0 => format!("k{index}"),
            _ if many => index.to_string(),
            0 => format!("pass{}word", run(random)),
            1 => format!("{}Token", run(random)),
            2 => text(random),
            _ => random.pick(&KEYS).to_owned(),
        };
        // The same name twice is one entry in JavaScript, wherever it comes: not what is looked for here.
        if used.contains(&name) {
            continue;
        }
        if !used.is_empty() {
            out.push(',');
        }
        out.push_str(&stringify(&Value::String(name.clone())));
        used.push(name);
        out.push(':');
        value(random, deeper.max(if many { 12 } else { 0 }), out);
    }
    out.push('}');
}

/// The cases, as the text of a file of them.
fn generate(count: usize, seed: u64) -> String {
    let mut random = Random(seed | 1);
    let mut out = String::from("[");
    for index in 0..count {
        if index > 0 {
            out.push(',');
        }
        let _ = write!(out, "{{\"name\":\"generated {index}\"");
        match random.below(12) {
            0 => out.push_str(",\"options\":{\"redact\":{\"secret\":false}}"),
            1 => out.push_str(",\"options\":{\"redact\":{\"email\":false,\"ip\":false}}"),
            2 => out.push_str(",\"options\":{\"redactKeys\":[\"name\",\"note\"]}"),
            _ => {}
        }
        out.push_str(",\"call\":{");
        let mut first = true;
        let mut field = |out: &mut String, name: &str| {
            if !std::mem::take(&mut first) {
                out.push(',');
            }
            let _ = write!(out, "\"{name}\":");
        };
        if random.chance(70) {
            field(&mut out, "input");
            value(&mut random, 0, &mut out);
        }
        if random.chance(70) {
            field(&mut out, "result");
            if random.chance(50) {
                // A result as MCP gives it.
                out.push_str("{\"content\":[{\"type\":\"text\",\"text\":");
                out.push_str(&stringify(&Value::String(text(&mut random))));
                out.push_str("}],\"structuredContent\":");
                value(&mut random, 0, &mut out);
                out.push('}');
            } else {
                value(&mut random, 0, &mut out);
            }
        }
        if random.chance(10) {
            field(&mut out, "toolDescription");
            out.push_str(&stringify(&Value::String(text(&mut random))));
        }
        if random.chance(10) {
            field(&mut out, "serverInstructions");
            out.push_str(&stringify(&Value::String(text(&mut random))));
        }
        if random.chance(8) {
            field(&mut out, "status");
            out.push_str("\"error\"");
            field(&mut out, "error");
            out.push_str("{\"category\":\"tool_error\",\"message\":");
            out.push_str(&stringify(&Value::String(text(&mut random))));
            out.push('}');
        }
        out.push_str("}}");
    }
    out.push(']');
    out
}

/// An object large enough to use up much of an event's scan budget, or all of it.
fn heavy(random: &mut Random, out: &mut String) {
    out.push('{');
    for index in 0..random.within(8, 70) {
        if index > 0 {
            out.push(',');
        }
        let length = random.within(6_000, 40_000);
        let _ = write!(out, "\"k{index}\":{{\"$prose\":{length}}}");
    }
    out.push('}');
}

/// A value, sometimes a heavy one.
fn weighed(random: &mut Random, out: &mut String) {
    if random.chance(12) {
        heavy(random, out);
    } else {
        value(random, 0, out);
    }
}

/// Text as JSON, sometimes longer than any field keeps.
fn said(random: &mut Random, out: &mut String) {
    if random.chance(10) {
        let _ = write!(out, "{{\"$prose\":{}}}", random.within(15_000, 60_000));
    } else {
        out.push_str(&stringify(&Value::String(text(random))));
    }
}

/// An agent's turn as `capture()` is given one, with what it is given wrongly too.
fn agent(random: &mut Random, out: &mut String) {
    out.push('{');
    let mut first = true;
    let mut field = |out: &mut String, name: &str| {
        if !std::mem::take(&mut first) {
            out.push(',');
        }
        let _ = write!(out, "\"{name}\":");
    };
    // Its IDs are the call's, never part of the agent's turn.
    if random.chance(30) {
        field(out, "orgId");
        out.push_str("\"org_1\"");
        field(out, "conversationId");
        out.push_str("\"conv_42\"");
    }
    for name in ["question", "answer"] {
        if random.chance(70) {
            field(out, name);
            said(random, out);
        }
    }
    if random.chance(60) {
        field(out, "turnId");
        out.push_str(random.pick(&[
            "\"7\"",
            "7",
            "\"\"",
            "1.5",
            "{\"a\":1}",
            "null",
            "\"turn é\"",
            "9007199254740993",
        ]));
    }
    if random.chance(40) {
        field(out, "status");
        out.push_str(random.pick(&["\"completed\"", "\"error\"", "\"bogus\"", "3"]));
    }
    if random.chance(30) {
        field(out, "error");
        match random.below(4) {
            0 => said(random, out),
            1 => {
                out.push_str("{\"message\":");
                said(random, out);
                out.push_str(",\"type\":\"Timeout\"}");
            }
            2 => out.push_str("{\"type\":\"Timeout\"}"),
            _ => out.push('5'),
        }
    }
    if random.chance(60) {
        field(out, "toolCalls");
        out.push('[');
        for index in 0..random.below(5) {
            if index > 0 {
                out.push(',');
            }
            if random.chance(8) {
                out.push_str(random.pick(&["7", "null", "\"search\"", "{\"input\":{}}"]));
                continue;
            }
            let _ = write!(
                out,
                "{{\"name\":{}",
                random.pick(&["\"search_docs\"", "\"find_order\"", "\"\"", "\"é\""])
            );
            if random.chance(70) {
                out.push_str(",\"input\":");
                weighed(random, out);
            }
            if random.chance(30) {
                let _ = write!(out, ",\"resultChars\":{}", random.below(5_000));
            }
            if random.chance(20) {
                out.push_str(",\"result\":");
                weighed(random, out);
            }
            if random.chance(30) {
                out.push_str(",\"resultIsError\":");
                out.push_str(random.pick(&["true", "false", "\"yes\""]));
            }
            if random.chance(30) {
                out.push_str(",\"errorMessage\":");
                said(random, out);
            }
            if random.chance(30) {
                let _ = write!(out, ",\"durationMs\":{}", random.below(90_000));
            }
            if random.chance(10) {
                out.push_str(",\"unknown\":1");
            }
            out.push('}');
        }
        out.push(']');
    }
    if random.chance(40) {
        field(out, "usage");
        out.push_str(random.pick(&[
            "{\"inputTokens\":812,\"outputTokens\":64,\"costUsd\":0.0031}",
            "{\"inputTokens\":\"many\",\"outputTokens\":1e21}",
            "{}",
            "7",
        ]));
    }
    for name in ["latency", "verification"] {
        if random.chance(30) {
            field(out, name);
            weighed(random, out);
        }
    }
    if random.chance(30) {
        field(out, "client");
        out.push_str(random.pick(&[
            "\"web\"",
            "{\"name\":\"web\",\"version\":\"2.4.0\"}",
            "{\"version\":\"2.4.0\"}",
            "\"\"",
            "5",
        ]));
    }
    if random.chance(20) {
        field(out, "apiKeyId");
        out.push_str(&stringify(&Value::String(text(random))));
        field(out, "surface");
        out.push_str(random.pick(&["\"support\"", "3", "\"\""]));
    }
    if random.chance(40) {
        field(out, "metadata");
        out.push_str(random.pick(&[
            "{\"plan\":\"pro\",\"seats\":12,\"trial\":false,\"note\":null}",
            "{\"plan\":\"pro\",\"nested\":{\"a\":1},\"list\":[1]}",
            "{\"10\":\"ten\",\"2\":\"two\",\"mail\":\"ann@example.com\"}",
            "\"not an object\"",
            "{}",
        ]));
    }
    out.push('}');
}

/// Calls with an agent part, as the text of a file of cases. Half are read as the wrapper reads
/// a call (`early`): the agent part when `capture()` is called inside it, with a scan budget of
/// its own, which is then charged to the call's.
fn generate_agents(count: usize, seed: u64) -> String {
    let mut random = Random(seed | 1);
    let mut out = String::from("[");
    // First, by hand, the ways an agent part read early and the rest of its call share the
    // call's scan budget: each `wide` reads about 24,000 of its 1,000,000 an entry.
    let wide = |entries: usize| {
        let entries: Vec<String> = (0..entries)
            .map(|index| format!("\"k{index}\":{{\"$prose\":30000}}"))
            .collect();
        format!("{{{}}}", entries.join(","))
    };
    let result = "{\"content\":[{\"type\":\"text\",\"text\":\"the result, for ann@example.com\"}],\"structuredContent\":{\"a\":{\"$prose\":9000},\"b\":{\"$prose\":9000}}}";
    for (name, input, agent) in [
        // The agent part uses its own budget up: the result gets none.
        (
            "uses its budget up",
            None,
            format!("{{\"latency\":{}}}", wide(60)),
        ),
        // It leaves the result a little, nothing, or enough: an entry reads all 30,000 of its
        // characters, so the budget's edge is between 33 and 34 of them.
        (
            "leaves a little",
            None,
            format!("{{\"latency\":{}}}", wide(33)),
        ),
        (
            "leaves nothing, just",
            None,
            format!("{{\"latency\":{}}}", wide(34)),
        ),
        (
            "leaves less than the result reads",
            None,
            format!(
                "{{\"latency\":{},\"verification\":{{\"a\":{{\"$prose\":9000}}}}}}",
                wide(32)
            ),
        ),
        (
            "leaves enough",
            None,
            format!("{{\"latency\":{}}}", wide(30)),
        ),
        // The arguments used the call's budget up before it: read on its own, it is whole.
        (
            "after arguments that used it up",
            Some(wide(60)),
            "{\"answer\":\"kept\",\"verification\":{\"ok\":true}}".to_owned(),
        ),
        // Together they use it up, neither alone.
        (
            "with arguments, more than the budget",
            Some(wide(25)),
            format!("{{\"question\":\"q\",\"latency\":{}}}", wide(25)),
        ),
        (
            "with arguments, within the budget",
            Some(wide(10)),
            format!(
                "{{\"toolCalls\":[{{\"name\":\"t\",\"input\":{}}}]}}",
                wide(10)
            ),
        ),
    ] {
        let _ = write!(
            out,
            "{{\"name\":\"agent budget: {name}\",\"early\":true,\"call\":{{\"agent\":{agent}"
        );
        if let Some(input) = input {
            let _ = write!(out, ",\"input\":{input}");
        }
        let _ = write!(out, ",\"result\":{result}}}}},");
    }
    for index in 0..count {
        if index > 0 {
            out.push(',');
        }
        let early = random.chance(50);
        let with_input = random.chance(70);
        let _ = write!(out, "{{\"name\":\"generated agent {index}\"");
        if early {
            out.push_str(",\"early\":true");
        }
        if random.chance(15) {
            out.push_str(",\"options\":{\"redactKeys\":[\"name\",\"note\"]}");
        }
        out.push_str(",\"call\":{\"agent\":");
        agent(&mut random, &mut out);
        if with_input {
            out.push_str(",\"input\":");
            // The wrapper's arguments are an object.
            if random.chance(15) {
                heavy(&mut random, &mut out);
            } else {
                out.push_str("{\"query\":");
                value(&mut random, 0, &mut out);
                let _ = write!(out, ",\"{}\":", random.pick(&["password", "note", "7"]));
                value(&mut random, 0, &mut out);
                out.push('}');
            }
        }
        if random.chance(70) {
            out.push_str(",\"result\":");
            if random.chance(50) {
                out.push_str("{\"content\":[{\"type\":\"text\",\"text\":");
                said(&mut random, &mut out);
                out.push_str("}],\"structuredContent\":");
                weighed(&mut random, &mut out);
                out.push('}');
            } else {
                weighed(&mut random, &mut out);
            }
        }
        if random.chance(30) {
            // identify's, which the agent part's is merged with.
            out.push_str(",\"metadata\":");
            out.push_str(random.pick(&[
                "{\"plan\":\"free\",\"region\":\"eu\"}",
                "{\"2\":\"zwei\",\"region\":\"eu\",\"nested\":{}}",
            ]));
        }
        if random.chance(20) {
            out.push_str(",\"toolDescription\":");
            said(&mut random, &mut out);
        }
        // One that is read early with arguments leaves the error out: the wrapper reads it
        // after the arguments, which is the one difference from reading a call at once.
        if (!early || !with_input) && random.chance(15) {
            out.push_str(
                ",\"status\":\"error\",\"error\":{\"category\":\"tool_error\",\"message\":",
            );
            said(&mut random, &mut out);
            out.push('}');
        }
        out.push_str("}}");
    }
    out.push(']');
    out
}

fn setting(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

#[test]
fn generated_inputs_are_written_as_typescript_writes_them() {
    let source = std::env::var_os("LSQUARE_TYPESCRIPT_SRC")
        .map_or_else(|| contract_dir().join("../lsquare/src"), PathBuf::from);
    if !source.join("event.ts").exists() {
        eprintln!(
            "skipped: the TypeScript SDK's source isn't at {}",
            source.display()
        );
        return;
    }
    if skip_without_node("identity with TypeScript's output on generated inputs") {
        return;
    }
    let plain = setting("LSQUARE_GENERATED_CASES", 1_500) as usize;
    let seed = setting("LSQUARE_GENERATED_SEED", 0x00C0_FFEE);
    // The calls as before, then a third as many again with an agent part.
    let with_agents = plain / 3;
    let cases = if with_agents == 0 {
        generate(plain, seed)
    } else {
        let (plain, agents) = (
            generate(plain, seed),
            generate_agents(with_agents, seed ^ 0xA6E7),
        );
        format!("{},{}", &plain[..plain.len() - 1], &agents[1..])
    };
    let file = std::env::temp_dir().join(format!(
        "lsquare-generated-{}-{seed}.json",
        std::process::id()
    ));
    std::fs::write(&file, &cases).expect("the generated cases are written");
    let out = node(
        TYPESCRIPT,
        &[
            file_url(&source),
            file.display().to_string(),
            stringify(&sdk()),
        ],
        "",
    );
    let _ = std::fs::remove_file(&file);
    let theirs: Vec<String> = serde_json::from_str(&out).expect("TypeScript's events");
    let Js::Array(ours) = parse(&cases) else {
        panic!("the generated cases aren't a list")
    };
    assert_eq!(ours.len(), theirs.len());
    // With the few made by hand.
    let count = ours.len();
    let mut differing = Vec::new();
    for (index, (case, theirs)) in ours.iter().zip(&theirs).enumerate() {
        let mine = match build(case).built {
            Err(code) => code,
            Ok(event) => stringify(&event),
        };
        if mine != *theirs {
            differing.push((index, mine, theirs.clone()));
        }
    }
    println!(
        "{} of {count} generated inputs (seed {seed}) are written byte for byte as TypeScript writes them",
        count - differing.len()
    );
    for (index, mine, theirs) in differing.iter().take(5) {
        // Where the two part, with a little of what surrounds it.
        let at = mine
            .char_indices()
            .zip(theirs.chars())
            .find(|((_, ours), theirs)| ours != theirs)
            .map_or(mine.len().min(theirs.len()), |((at, _), _)| at);
        let around = |text: &str| -> String {
            let from = (0..=at.saturating_sub(120))
                .rev()
                .find(|index| text.is_char_boundary(*index))
                .unwrap_or(0);
            text[from..].chars().take(300).collect()
        };
        println!(
            "generated {index} differs at byte {at}:\n  rust:       …{}\n  typescript: …{}",
            around(mine),
            around(theirs)
        );
    }
    assert!(
        differing.is_empty(),
        "{} of {count} generated inputs differ from TypeScript's events (seed {seed}): cases {:?}",
        differing.len(),
        differing
            .iter()
            .map(|(index, _, _)| *index)
            .take(40)
            .collect::<Vec<_>>()
    );
}
