//! Choosing the held-out sources: their question/answer examples go to the test file, while the world book (which has everything) stays in
//! the training file, so the test asks about things the model was shown as text but never as a question.
use std::collections::HashSet;

use sha2::{Digest, Sha256};

/// About `percent` of `sources` (at least one when there are 5 or more and percent > 0), chosen by a seeded hash: the same
/// world, seed and percent always choose the same ones, and adding a source does not reshuffle the others.
pub fn held_out_sources(sources: &[String], seed: u64, percent: u8) -> HashSet<String> {
    let mut unique: Vec<&String> = sources.iter().collect::<HashSet<_>>().into_iter().collect();
    unique.sort();
    let want = if percent == 0 { 0 } else { ((unique.len() * percent as usize + 50) / 100).max(if unique.len() >= 5 { 1 } else { 0 }) };
    let mut ranked: Vec<(u64, String)> = unique.iter().map(|s| {
        let d = Sha256::digest(format!("{seed}|{s}").as_bytes());
        (u64::from_be_bytes([d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]]), (*s).clone())
    }).collect();
    ranked.sort();
    ranked.into_iter().take(want).map(|(_, s)| s).collect()
}
