//! What a client learns from `identify`: which ID fields its last answers gave, and when.
//!
//! When `identify` fails for a call, the call is sent whole only if a `capture()` inside it gave
//! every ID field `identify` last returned: the fields required of it are those in effect when
//! the call failed, whenever its event is built, and those of the latest answer as well. So the
//! last answers are kept with the time each returned, taken as it is noted: an answer noted
//! later never has an earlier time.

use std::collections::VecDeque;
use std::sync::{Mutex, PoisonError};
use std::time::Instant;

/// An answer's ID fields, as bits.
pub(crate) const ORG: u8 = 1;
/// See [`ORG`].
pub(crate) const USER: u8 = 2;
/// See [`ORG`].
pub(crate) const CONVERSATION: u8 = 4;
const ALL: u8 = ORG | USER | CONVERSATION;

/// The answers kept. Once older ones are let go of, what held before the oldest kept isn't known,
/// and every ID field is required of a call that failed then.
const KEPT: usize = 32;

/// The answers of a client's `identify`, on whichever of its servers.
#[derive(Default)]
pub(crate) struct Identified {
    history: Mutex<History>,
}

#[derive(Default)]
struct History {
    /// The last answers that came in time, oldest first, each with when it returned.
    answers: VecDeque<(Instant, u8)>,
    /// Older answers were let go of.
    forgot: bool,
}

/// Each ID field one of two requirements asks for, where none (no answer yet) asks for a userId:
/// none when both are.
pub(crate) fn both(a: Option<u8>, b: Option<u8>) -> Option<u8> {
    match (a, b) {
        (None, None) => None,
        (a, b) => Some(a.unwrap_or(USER) | b.unwrap_or(USER)),
    }
}

impl Identified {
    fn lock(&self) -> std::sync::MutexGuard<'_, History> {
        self.history.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The time, as answers take theirs: never before an answer noted already, whatever the
    /// clock says, and no answer noted later is before it.
    pub(crate) fn now(&self) -> Instant {
        Self::time(&self.lock())
    }

    fn time(history: &History) -> Instant {
        let now = Instant::now();
        history
            .answers
            .back()
            .map_or(now, |(last, _)| now.max(*last))
    }

    /// An `identify` returned, now. `learn` is asked, with that time, whether its answer counts:
    /// it came in time and gave valid IDs, `fields`. One that counts is noted there and then, so
    /// whatever is looked up for a time before now is final. Returns the time.
    pub(crate) fn returned(&self, fields: u8, learn: impl FnOnce(Instant) -> bool) -> Instant {
        let mut history = self.lock();
        let at = Self::time(&history);
        if learn(at) {
            if history.answers.len() == KEPT {
                history.answers.pop_front();
                history.forgot = true;
            }
            history.answers.push_back((at, fields));
        }
        at
    }

    /// The ID fields of the latest answer: none before any.
    pub(crate) fn latest(&self) -> Option<u8> {
        self.lock().answers.back().map(|(_, fields)| *fields)
    }

    /// The ID fields required of a call that failed at `at`: those of the answer in effect then.
    /// None: there was no answer yet. At the very instant of an answer either it or the one
    /// before was in effect, and both count; of a time whose answers were let go of, every field.
    pub(crate) fn at(&self, at: Instant) -> Option<u8> {
        let history = self.lock();
        // What held before the oldest answer kept.
        let before = if history.forgot { Some(ALL) } else { None };
        let mut required = before;
        let mut earlier = before;
        let mut instant = None;
        for (returned, fields) in &history.answers {
            if *returned > at {
                break;
            }
            if instant != Some(*returned) {
                earlier = required;
                instant = Some(*returned);
            }
            required = Some(*fields);
        }
        if instant != Some(at) {
            return required;
        }
        // Every answer of that instant, and what held as it began.
        let same = history
            .answers
            .iter()
            .filter(|(returned, _)| *returned == at)
            .fold(0, |all, (_, fields)| all | fields);
        Some(same | earlier.unwrap_or(USER))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    /// A history with answers at chosen times.
    fn history(answers: &[(Instant, u8)], forgot: bool) -> Identified {
        Identified {
            history: Mutex::new(History {
                answers: answers.iter().copied().collect(),
                forgot,
            }),
        }
    }

    #[test]
    fn a_failed_call_needs_what_was_in_effect_then() {
        let start = Instant::now();
        let at = |milliseconds: u64| start + Duration::from_millis(milliseconds);
        let known = history(
            &[(at(10), ORG | USER), (at(20), 0), (at(30), CONVERSATION)],
            false,
        );
        // Before any answer, none: a userId is asked for.
        assert_eq!(known.at(at(5)), None);
        assert_eq!(known.at(at(15)), Some(ORG | USER));
        // An answer without IDs asks for none, and a later one doesn't change what held before.
        assert_eq!(known.at(at(25)), Some(0));
        assert_eq!(known.at(at(35)), Some(CONVERSATION));
        assert_eq!(known.latest(), Some(CONVERSATION));
        assert_eq!(history(&[], false).latest(), None);
        assert_eq!(history(&[], false).at(at(5)), None);
    }

    #[test]
    fn at_the_instant_of_an_answer_it_and_the_one_before_both_count() {
        let start = Instant::now();
        let at = |milliseconds: u64| start + Duration::from_millis(milliseconds);
        let known = history(&[(at(10), ORG), (at(20), 0), (at(20), CONVERSATION)], false);
        assert_eq!(known.at(at(20)), Some(ORG | CONVERSATION));
        // After it, the last one noted.
        assert_eq!(known.at(at(21)), Some(CONVERSATION));
        // The first answer ever: until it, a userId was asked for.
        assert_eq!(known.at(at(10)), Some(ORG | USER));
    }

    #[test]
    fn a_time_whose_answers_were_let_go_of_needs_every_field() {
        let start = Instant::now();
        let at = |milliseconds: u64| start + Duration::from_millis(milliseconds);
        let known = history(&[(at(10), 0), (at(20), USER)], true);
        assert_eq!(known.at(at(5)), Some(ALL));
        assert_eq!(known.at(at(10)), Some(ALL));
        assert_eq!(known.at(at(15)), Some(0));
        assert_eq!(known.at(at(25)), Some(USER));
    }

    #[test]
    fn only_an_answer_that_counts_is_noted_and_the_oldest_give_way() {
        let known = Identified::default();
        let first = known.returned(ORG, |_| false);
        assert_eq!(known.latest(), None);
        for _ in 0..KEPT {
            known.returned(USER, |_| true);
        }
        assert_eq!(known.latest(), Some(USER));
        assert_eq!(known.at(first - Duration::from_millis(1)), None);
        let last = known.returned(CONVERSATION, |at| at >= first);
        assert_eq!(known.latest(), Some(CONVERSATION));
        // One was let go of: what held before the oldest kept isn't known any more.
        assert_eq!(known.at(first - Duration::from_millis(1)), Some(ALL));
        assert!(known.at(last + Duration::from_millis(1)) == Some(CONVERSATION));
    }

    #[test]
    fn two_requirements_ask_for_every_field_of_either() {
        assert_eq!(both(None, None), None);
        assert_eq!(both(Some(0), None), Some(USER));
        assert_eq!(
            both(Some(ORG), Some(CONVERSATION)),
            Some(ORG | CONVERSATION)
        );
        assert_eq!(both(Some(0), Some(0)), Some(0));
    }
}
