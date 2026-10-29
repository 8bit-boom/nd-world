//! Python's `difflib.unified_diff` (and the `SequenceMatcher` under it, `autojunk` on, no junk function) - the same hunks, line for
//! line, as CPython 3.13 writes them.

use std::collections::HashMap;

type Block = (usize, usize, usize);

struct Matcher<'a> {
    a: &'a [&'a str],
    b: &'a [&'a str],
    b2j: HashMap<&'a str, Vec<usize>>,
}

impl<'a> Matcher<'a> {
    fn new(a: &'a [&'a str], b: &'a [&'a str]) -> Self {
        let mut b2j: HashMap<&str, Vec<usize>> = HashMap::new();
        for (i, e) in b.iter().enumerate() {
            b2j.entry(e).or_default().push(i);
        }
        // autojunk: with 200 or more lines, a line that is more than 1% of them (+1) is "popular" and never starts a match
        if b.len() >= 200 {
            let ntest = b.len() / 100 + 1;
            b2j.retain(|_, idxs| idxs.len() <= ntest);
        }
        Matcher { a, b, b2j }
    }

    fn longest(&self, alo: usize, ahi: usize, blo: usize, bhi: usize) -> Block {
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for i in alo..ahi {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            if let Some(js) = self.b2j.get(self.a[i]) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = if j > 0 { j2len.get(&(j - 1)).copied().unwrap_or(0) } else { 0 } + 1;
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        // extend the match over lines the index skipped (popular ones), both ways
        while besti > alo && bestj > blo && self.a[besti - 1] == self.b[bestj - 1] {
            besti -= 1;
            bestj -= 1;
            bestsize += 1;
        }
        while besti + bestsize < ahi && bestj + bestsize < bhi && self.a[besti + bestsize] == self.b[bestj + bestsize] {
            bestsize += 1;
        }
        (besti, bestj, bestsize)
    }

    fn matching_blocks(&self) -> Vec<Block> {
        let (la, lb) = (self.a.len(), self.b.len());
        let mut queue = vec![(0, la, 0, lb)];
        let mut found: Vec<Block> = vec![];
        while let Some((alo, ahi, blo, bhi)) = queue.pop() {
            let (i, j, k) = self.longest(alo, ahi, blo, bhi);
            if k > 0 {
                found.push((i, j, k));
                if alo < i && blo < j {
                    queue.push((alo, i, blo, j));
                }
                if i + k < ahi && j + k < bhi {
                    queue.push((i + k, ahi, j + k, bhi));
                }
            }
        }
        found.sort();
        let (mut i1, mut j1, mut k1) = (0, 0, 0);
        let mut out: Vec<Block> = vec![];
        for (i2, j2, k2) in found {
            if i1 + k1 == i2 && j1 + k1 == j2 {
                k1 += k2;
            } else {
                if k1 > 0 {
                    out.push((i1, j1, k1));
                }
                (i1, j1, k1) = (i2, j2, k2);
            }
        }
        if k1 > 0 {
            out.push((i1, j1, k1));
        }
        out.push((la, lb, 0));
        out
    }

    fn opcodes(&self) -> Vec<(&'static str, usize, usize, usize, usize)> {
        let (mut i, mut j) = (0, 0);
        let mut out = vec![];
        for (ai, bj, size) in self.matching_blocks() {
            let tag = if i < ai && j < bj { "replace" } else if i < ai { "delete" } else if j < bj { "insert" } else { "" };
            if !tag.is_empty() {
                out.push((tag, i, ai, j, bj));
            }
            i = ai + size;
            j = bj + size;
            if size > 0 {
                out.push(("equal", ai, i, bj, j));
            }
        }
        out
    }

    fn grouped(&self, n: usize) -> Vec<Vec<(&'static str, usize, usize, usize, usize)>> {
        let mut codes = self.opcodes();
        if codes.is_empty() {
            codes.push(("equal", 0, 1, 0, 1));
        }
        if codes[0].0 == "equal" {
            let (tag, i1, i2, j1, j2) = codes[0];
            codes[0] = (tag, i1.max(i2.saturating_sub(n)), i2, j1.max(j2.saturating_sub(n)), j2);
        }
        let last = codes.len() - 1;
        if codes[last].0 == "equal" {
            let (tag, i1, i2, j1, j2) = codes[last];
            codes[last] = (tag, i1, i2.min(i1 + n), j1, j2.min(j1 + n));
        }
        let nn = n + n;
        let (mut groups, mut group) = (vec![], vec![]);
        for (tag, mut i1, i2, mut j1, j2) in codes {
            if tag == "equal" && i2 - i1 > nn {
                group.push((tag, i1, i2.min(i1 + n), j1, j2.min(j1 + n)));
                groups.push(std::mem::take(&mut group));
                i1 = i1.max(i2.saturating_sub(n));
                j1 = j1.max(j2.saturating_sub(n));
            }
            group.push((tag, i1, i2, j1, j2));
        }
        if !group.is_empty() && !(group.len() == 1 && group[0].0 == "equal") {
            groups.push(group);
        }
        groups
    }
}

fn range(start: usize, stop: usize) -> String {
    let mut beginning = start as i64 + 1;
    let length = stop - start;
    if length == 1 {
        return beginning.to_string();
    }
    if length == 0 {
        beginning -= 1;
    }
    format!("{beginning},{length}")
}

/// `"".join(difflib.unified_diff(a, b, fromfile=..., tofile=...))` (n=3, lineterm="\n", no dates)
pub fn unified_diff(a: &[&str], b: &[&str], fromfile: &str, tofile: &str) -> String {
    let m = Matcher::new(a, b);
    let mut out = String::new();
    let mut started = false;
    for group in m.grouped(3) {
        if !started {
            started = true;
            out.push_str(&format!("--- {fromfile}\n+++ {tofile}\n"));
        }
        let (first, last) = (group[0], group[group.len() - 1]);
        out.push_str(&format!("@@ -{} +{} @@\n", range(first.1, last.2), range(first.3, last.4)));
        for (tag, i1, i2, j1, j2) in group {
            if tag == "equal" {
                for l in &a[i1..i2] {
                    out.push(' ');
                    out.push_str(l);
                }
                continue;
            }
            if tag == "replace" || tag == "delete" {
                for l in &a[i1..i2] {
                    out.push('-');
                    out.push_str(l);
                }
            }
            if tag == "replace" || tag == "insert" {
                for l in &b[j1..j2] {
                    out.push('+');
                    out.push_str(l);
                }
            }
        }
    }
    out
}
