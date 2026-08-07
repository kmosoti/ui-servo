//! Span ids: the join key for the whole sensor stack.
//!
//! Every fragment root carries a `data-span-id`. The browser probe reports
//! against it, the beacon ingest stores it, and the gates correlate on it. It
//! therefore has to be (a) unique per render, (b) lexicographically sortable by
//! render time so a human reading a JSONL evidence file sees causality, and (c)
//! free of dependencies, because a build-time crate bump must never be able to
//! change the shape of the join key.
//!
//! The encoding is ULID-ish: 48 bits of millisecond timestamp followed by 80
//! bits of process-local randomness, rendered as 26 Crockford base32 characters.
//! It is not a canonical ULID (the randomness is an xorshift, not a CSPRNG) and
//! nothing here is security-bearing — it is a correlation id, and the docs say
//! so out loud rather than implying cryptographic strength.

use std::cell::Cell;
use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// Crockford base32 alphabet (no I, L, O, U — unambiguous when read aloud).
const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// Characters in an encoded span id: 128 bits at 5 bits per character.
const SPAN_ID_LEN: usize = 26;

static COUNTER: AtomicU64 = AtomicU64::new(0);

thread_local! {
    static RNG: Cell<u64> = Cell::new(seed());
}

/// A fresh span id for one fragment render.
pub fn new_span_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_millis() as u64)
        .unwrap_or(0);
    encode(millis & 0x0000_FFFF_FFFF_FFFF, next_u64())
}

/// 128 bits (48 of `millis`, 80 of `entropy` plus counter) as Crockford base32.
fn encode(millis: u64, entropy: u64) -> String {
    let low = u128::from(entropy) ^ (u128::from(COUNTER.fetch_add(1, Ordering::Relaxed)) << 64);
    let value = (u128::from(millis) << 80) | (low & ((1u128 << 80) - 1));

    let mut out = [b'0'; SPAN_ID_LEN];
    for (index, slot) in out.iter_mut().enumerate() {
        let shift = 5 * (SPAN_ID_LEN - 1 - index);
        *slot = CROCKFORD[((value >> shift) & 0x1F) as usize];
    }
    // Every byte came out of CROCKFORD, so this is ASCII by construction.
    String::from_utf8(out.to_vec()).expect("crockford alphabet is ascii")
}

/// xorshift64* — cheap, non-cryptographic, and plenty for a per-render id.
fn next_u64() -> u64 {
    RNG.with(|state| {
        let mut x = state.get();
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        state.set(x);
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    })
}

/// Per-thread seed: `RandomState` is already randomly keyed per process, so
/// hashing a monotonic nonce through it gives distinct streams per thread
/// without reaching for a random-number crate.
fn seed() -> u64 {
    let nonce = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_nanos() as u64)
        .unwrap_or(1);
    let mut hasher = RandomState::new().build_hasher();
    hasher.write_u64(nonce);
    hasher.write_u64(nanos);
    match hasher.finish() {
        0 => 0x9E37_79B9_7F4A_7C15, // xorshift is absorbing at zero
        value => value,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn span_ids_are_26_crockford_characters() {
        let id = new_span_id();
        assert_eq!(id.len(), SPAN_ID_LEN);
        assert!(id.bytes().all(|byte| CROCKFORD.contains(&byte)), "{id}");
    }

    #[test]
    fn span_ids_do_not_repeat() {
        let ids: std::collections::HashSet<String> = (0..10_000).map(|_| new_span_id()).collect();
        assert_eq!(ids.len(), 10_000);
    }

    #[test]
    fn span_ids_sort_by_render_time() {
        let earlier = encode(1, 0);
        let later = encode(2, 0);
        assert!(earlier < later, "{earlier} !< {later}");
    }
}
