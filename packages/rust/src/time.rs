//! Wall-clock times as events write them, without a date library: ISO 8601 in UTC, to the millisecond.

use std::sync::LazyLock;
use std::time::{SystemTime, UNIX_EPOCH};

use regex::Regex;

const DAY_MS: i64 = 86_400_000;

/// Milliseconds since the Unix epoch.
pub(crate) fn now_ms() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(since) => since.as_millis() as i64,
        Err(before) => -(before.duration().as_millis() as i64),
    }
}

/// Days since the epoch to a civil date (Howard Hinnant's algorithm).
fn civil(days: i64) -> (i64, i64, i64) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    (year_of_era + era * 400 + i64::from(month <= 2), month, day)
}

/// A civil date to days since the epoch.
fn days(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    era * 146_097 + year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year - 719_468
}

/// A time as JavaScript's `toISOString` writes it: `2026-10-28T00:00:00.000Z`.
pub(crate) fn iso(ms: i64) -> String {
    let (year, month, day) = civil(ms.div_euclid(DAY_MS));
    let of_day = ms.rem_euclid(DAY_MS);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        of_day / 3_600_000,
        of_day / 60_000 % 60,
        of_day / 1_000 % 60,
        of_day % 1_000
    )
}

static ISO: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?(Z|[+-][0-9]{2}:[0-9]{2})\z").expect("time pattern")
});

/// An ISO 8601 time with `Z` or an offset, in milliseconds since the epoch.
pub(crate) fn parse_iso(text: &str) -> Option<i64> {
    let found = ISO.captures(text)?;
    let number = |index: usize| {
        found
            .get(index)
            .map_or(Some(0), |digits| digits.as_str().parse::<i64>().ok())
    };
    let (year, month, day, hour, minute, second) = (
        number(1)?,
        number(2)?,
        number(3)?,
        number(4)?,
        number(5)?,
        number(6)?,
    );
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 24
        || minute > 59
        || second > 59
    {
        return None;
    }
    let fraction = found.get(7).map_or(0, |digits| {
        format!("{:0<3}", digits.as_str())[..3]
            .parse::<i64>()
            .unwrap_or(0)
    });
    let offset = match &found[8] {
        "Z" => 0,
        zone => {
            let minutes = zone[1..3].parse::<i64>().ok()? * 60 + zone[4..6].parse::<i64>().ok()?;
            if zone.starts_with('-') {
                -minutes
            } else {
                minutes
            }
        }
    };
    Some(
        days(year, month, day) * DAY_MS
            + hour * 3_600_000
            + minute * 60_000
            + second * 1_000
            + fraction
            - offset * 60_000,
    )
}

/// The next 00:00 UTC after `ms`.
pub(crate) fn next_midnight(ms: i64) -> i64 {
    (ms.div_euclid(DAY_MS) + 1) * DAY_MS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn times_are_written_and_read_as_iso_8601() {
        assert_eq!(iso(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(iso(1_793_145_600_000), "2026-10-28T00:00:00.000Z");
        assert_eq!(iso(951_782_400_123), "2000-02-29T00:00:00.123Z");
        assert_eq!(iso(-1), "1969-12-31T23:59:59.999Z");
        for text in [
            "2026-10-28T00:00:00.000Z",
            "2000-02-29T00:00:00.123Z",
            "1969-12-31T23:59:59.999Z",
        ] {
            assert_eq!(iso(parse_iso(text).unwrap()), text);
        }
        assert_eq!(
            parse_iso("2026-10-28T02:00:00+02:00"),
            parse_iso("2026-10-28T00:00:00Z")
        );
        assert_eq!(parse_iso("2026-10-28T00:00Z"), Some(1_793_145_600_000));
        assert_eq!(parse_iso("tomorrow"), None);
        assert_eq!(
            iso(next_midnight(1_793_145_600_001)),
            "2026-10-29T00:00:00.000Z"
        );
    }
}
