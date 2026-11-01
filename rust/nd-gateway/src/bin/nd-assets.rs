//! `nd-assets <static dir> <app/maps dir>`: the image build's pictures, tile pyramids, minified CSS and compressed twins (src/assets.rs).
use std::path::PathBuf;

fn main() {
    let mut args = std::env::args().skip(1);
    let (Some(static_dir), Some(maps)) = (args.next().map(PathBuf::from), args.next().map(PathBuf::from)) else {
        eprintln!("usage: nd-assets <static dir> <app/maps dir>");
        std::process::exit(2);
    };
    let (n, before, after) = nd_gateway::assets::compact_maps(&static_dir.join("maps"));
    println!("bundled maps as avif (files, bytes before, after): ({n}, {before}, {after})");
    println!("static thumbnails written: {}", nd_gateway::assets::thumbnails(&[static_dir.join("maps"), static_dir.join("races")]));
    println!("map pyramids: {}", nd_gateway::assets::tiles(&static_dir, &maps));
    println!("minify css (files, bytes in, out): {:?}", nd_gateway::assets::minify_tree(&static_dir));
    println!("precompress (files, br, gz): {:?}", nd_gateway::assets::precompress(&static_dir));
}
