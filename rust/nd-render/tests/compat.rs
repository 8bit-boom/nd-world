//! How many of the app's templates does MiniJinja accept? (Compile only - rendering needs the data and the filters.)
use std::path::PathBuf;

#[test]
fn survey() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../app/templates");
    let env = nd_render::environment(&dir, &dir);
    let mut ok = 0;
    let mut bad: Vec<(String, String)> = vec![];
    let mut names = vec![];
    walk(&dir, &dir, &mut names);
    names.sort();
    for n in &names {
        match env.get_template(n) {
            Ok(_) => ok += 1,
            Err(e) => bad.push((n.clone(), format!("{e:#}").lines().next().unwrap_or("").to_string())),
        }
    }
    println!("compiled: {ok} of {}", names.len());
    for (n, e) in &bad {
        println!("FAIL {n}: {e}");
    }
}

fn walk(root: &std::path::Path, dir: &std::path::Path, out: &mut Vec<String>) {
    for e in std::fs::read_dir(dir).unwrap().filter_map(|e| e.ok()) {
        let p = e.path();
        if p.is_dir() {
            walk(root, &p, out);
        } else if p.extension().map_or(false, |x| x == "html" || x == "xml" || x == "txt") {
            out.push(p.strip_prefix(root).unwrap().to_string_lossy().to_string());
        }
    }
}
