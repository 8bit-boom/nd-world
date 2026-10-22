//! app/imaging.py for uploads (phase 3): convert an uploaded picture to the configured format (Settings -> System: static_format) and
//! write its grid thumbnail in the preview format (AVIF at quality 60 or WebP at 74, at most 440 px, Pillow's size rounding).
//! The encoders are not Pillow's (ravif for AVIF, libwebp for WebP, the image crate for PNG / JPEG), so the bytes differ from what
//! Python writes; the format, the file names, the pixel size and the alpha channel are the same. What this port does not do the way
//! Pillow does - animated pictures, GIF palettes, AVIF sources, a picture it cannot decode - returns None, and the request is Python's.

use std::io::Read;
use std::path::{Path, PathBuf};

use image::{DynamicImage, ImageEncoder};

const CONVERTIBLE: [&str; 6] = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"];
const CONVERT_QUALITY: u8 = 90;
const THUMBNAIL_MAX_DIM: u32 = 440;

fn suffix_lower(p: &Path) -> String {
    p.extension().map(|e| format!(".{}", e.to_string_lossy().to_ascii_lowercase())).unwrap_or_default()
}

/// A picture this port reads exactly as Pillow would: not animated, not a GIF (Pillow keeps its palette mode), not AVIF
fn readable(path: &Path) -> Option<DynamicImage> {
    let ext = suffix_lower(path);
    if !CONVERTIBLE.contains(&ext.as_str()) || ext == ".gif" || ext == ".avif" {
        return None;
    }
    let mut head = vec![0u8; 64 * 1024];
    let n = std::fs::File::open(path).ok()?.read(&mut head).ok()?;
    let head = &head[..n];
    // animation: an APNG's acTL chunk, a WebP's ANIM chunk (Pillow's is_animated) - those are Python's
    if head.windows(4).any(|w| w == b"acTL") || (head.starts_with(b"RIFF") && head.windows(4).any(|w| w == b"ANIM")) {
        return None;
    }
    let format = image::ImageFormat::from_path(path).ok()?;
    let reader = image::ImageReader::with_format(std::io::BufReader::new(std::fs::File::open(path).ok()?), format);
    let img = reader.decode().ok()?;
    // Pillow's PNG / JPEG / WebP modes map onto these; 16-bit and float sources are converted by Pillow in ways not copied here
    use image::ColorType::*;
    match img.color() {
        L8 | La8 | Rgb8 | Rgba8 => Some(img),
        _ => None,
    }
}

/// `_frame_mode`: RGBA when the picture has an alpha channel (and the target can hold one), else RGB
fn frame(img: &DynamicImage, jpeg: bool) -> DynamicImage {
    if !jpeg && img.color().has_alpha() {
        DynamicImage::ImageRgba8(img.to_rgba8())
    } else {
        DynamicImage::ImageRgb8(img.to_rgb8())
    }
}

fn encode(img: &DynamicImage, format: &str, quality: u8, dest: &Path) -> Option<()> {
    let (w, h) = (img.width(), img.height());
    let bytes: Vec<u8> = match format {
        "avif" => {
            let mut out = vec![];
            let enc = image::codecs::avif::AvifEncoder::new_with_speed_quality(&mut out, 6, quality);
            enc.write_image(img.as_bytes(), w, h, img.color().into()).ok()?;
            out
        }
        "webp" => {
            let enc = match img {
                DynamicImage::ImageRgba8(b) => webp::Encoder::from_rgba(b.as_raw(), w, h),
                DynamicImage::ImageRgb8(b) => webp::Encoder::from_rgb(b.as_raw(), w, h),
                _ => return None,
            };
            enc.encode_simple(false, quality as f32).ok()?.to_vec()
        }
        "png" => {
            let mut out = vec![];
            let enc = image::codecs::png::PngEncoder::new_with_quality(&mut out, image::codecs::png::CompressionType::Best, image::codecs::png::FilterType::Adaptive);
            enc.write_image(img.as_bytes(), w, h, img.color().into()).ok()?;
            out
        }
        "jpg" => {
            let mut out = vec![];
            let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, quality);
            enc.write_image(img.as_bytes(), w, h, img.color().into()).ok()?;
            out
        }
        _ => return None,
    };
    std::fs::write(dest, bytes).ok()
}

/// What a picture upload leaves behind: the picture's final path, and every file written (to remove them all if the request
/// goes to Python after all)
pub struct Finished {
    pub path: PathBuf,
    pub written: Vec<PathBuf>,
    pub remove: Option<PathBuf>,
}

/// `convert_image(path, static_format, animated_format)` then `make_thumbnail(path)` - None when this port would not do it as Pillow
/// does (the request is then Python's). The original is listed in `remove` (delete it once the request is answered).
pub fn finish_upload(path: &Path, static_format: &str, preview_format: &str) -> Option<Finished> {
    let img = readable(path)?;
    let (target, ext) = match static_format {
        "avif" => ("avif", "avif"),
        "webp" => ("webp", "webp"),
        "png" => ("png", "png"),
        "jpg" | "jpeg" => ("jpg", "jpg"),
        _ => ("", ""), // "none" and anything else: kept as uploaded
    };
    let mut written = vec![];
    let mut remove = None;
    let mut out = path.to_path_buf();
    if !target.is_empty() {
        let dest = path.with_extension(ext);
        if dest != path {
            let quality = CONVERT_QUALITY;
            let framed = frame(&img, target == "jpg");
            written.push(dest.clone());
            encode(&framed, target, quality, &dest)?;
            remove = Some(path.to_path_buf());
            out = dest;
        }
    }
    // the thumbnail, from the converted picture's pixels as Pillow reads them back (same size and alpha as `img`)
    let (pil, suffix, q) = match preview_format {
        "webp" | "original" => ("webp", "_thumb.webp", 74u8),
        _ => ("avif", "_thumb.avif", 60u8),
    };
    let stem = out.file_stem()?.to_string_lossy().to_string();
    let thumb = out.with_file_name(format!("{stem}{suffix}"));
    // Python makes it from the converted file: a JPEG has no alpha left
    let framed = frame(&img, target == "jpg");
    let framed = frame(&framed, false);
    let small = match thumbnail_size(framed.width(), framed.height(), THUMBNAIL_MAX_DIM) {
        Some((w, h)) => framed.resize_exact(w, h, image::imageops::FilterType::Lanczos3),
        None => framed,
    };
    written.push(thumb.clone());
    encode(&small, pil, q, &thumb)?;
    let other = out.with_file_name(format!("{stem}{}", if suffix == "_thumb.avif" { "_thumb.webp" } else { "_thumb.avif" }));
    let _ = std::fs::remove_file(other);
    Some(Finished { path: out, written, remove })
}

/// Pillow's `Image.thumbnail((max, max))` size: None when the picture already fits (no resize)
pub fn thumbnail_size(w: u32, h: u32, max: u32) -> Option<(u32, u32)> {
    let (x, y) = (max as f64, max as f64);
    if x >= w as f64 && y >= h as f64 {
        return None;
    }
    let aspect = w as f64 / h as f64;
    let round_aspect = |number: f64, key: &dyn Fn(f64) -> f64| -> f64 {
        let (lo, hi) = (number.floor(), number.ceil());
        // min((floor, ceil), key=...) keeps the first on a tie
        let best = if key(hi) < key(lo) { hi } else { lo };
        best.max(1.0)
    };
    if x / y >= aspect {
        let nx = round_aspect(y * aspect, &|n| (aspect - n / y).abs());
        Some((nx as u32, y as u32))
    } else {
        let ny = round_aspect(x / aspect, &|n| if n == 0.0 { 0.0 } else { (aspect - x / n).abs() });
        Some((x as u32, ny as u32))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thumbnail_sizes_round_like_pillow() {
        assert_eq!(thumbnail_size(400, 300, 440), None);
        assert_eq!(thumbnail_size(1000, 1000, 440), Some((440, 440)));
        assert_eq!(thumbnail_size(1920, 1080, 440), Some((440, 248)));
        assert_eq!(thumbnail_size(1080, 1920, 440), Some((247, 440)));
        assert_eq!(thumbnail_size(5000, 3, 440), Some((440, 1)));
        assert_eq!(thumbnail_size(441, 440, 440), Some((440, 439)));
        // 400 random sizes, against Pillow 12.3's Image.thumbnail((440, 440))
        for (w, h, tw, th) in [(2653,1236,440,205),(3235,5333,267,440),(396,594,293,440),(4390,772,440,77),(2996,4775,276,440),(476,4157,50,440),(1759,308,440,77),(705,3553,87,440),(3426,573,440,74),(1972,744,440,166),(4515,3478,440,339),(485,4633,46,440),(1015,1829,244,440),(5167,5140,440,438),(4776,507,440,47),(4728,4797,434,440),(3250,407,440,55),(1812,382,440,93),(4561,1091,440,105),(2373,3434,304,440),(1182,4430,117,440),(965,4677,91,440),(2528,4590,242,440),(5587,1481,440,117),(845,4765,78,440),(4680,5234,393,440),(1540,3051,222,440),(799,4488,78,440),(5834,515,440,39),(4624,489,440,47),(5071,1688,440,146),(4067,5574,321,440),(4356,3503,440,354),(2574,3815,297,440),(4797,3713,440,341),(2963,2456,440,365),(2036,1473,440,318),(5727,2000,440,154),(671,4706,63,440),(2460,4303,252,440),(4056,2814,440,305),(5976,3677,440,271),(2359,4989,208,440),(600,968,273,440),(4194,3426,440,359),(1352,2803,212,440),(1246,4006,137,440),(3455,322,440,41),(5475,636,440,51),(4572,4695,428,440),(2571,2787,406,440),(5696,2869,440,222),(4870,4069,440,368),(4751,3738,440,346),(564,767,324,440),(2212,3884,251,440),(5711,5441,440,419),(533,498,440,411),(5990,5747,440,422),(2537,5302,211,440),(4735,5581,373,440),(3651,2332,440,281),(5871,3161,440,237),(5478,2843,440,228),(185,3783,22,440),(2912,1377,440,208),(5005,960,440,84),(4045,483,440,53),(1788,2355,334,440),(1060,2029,230,440),(3260,3203,440,432),(4068,661,440,71),(1363,3680,163,440),(3291,4502,322,440),(2277,1122,440,217),(3527,4508,344,440),(2281,5787,173,440),(3403,2940,440,380),(5593,3117,440,245),(1891,1237,440,288),(680,1444,207,440),(1240,1901,287,440),(5395,1912,440,156),(99,3973,11,440),(4827,1494,440,136),(2153,2310,410,440),(34,1194,13,440),(3433,4380,345,440),(3025,4996,266,440),(4640,2611,440,248),(1029,5657,80,440),(4223,5060,367,440),(5366,5540,426,440),(443,3741,52,440),(5576,4582,440,362),(3215,3261,434,440),(3269,3229,440,435),(849,3945,95,440),(5197,3281,440,278),(510,1562,144,440),(552,1711,142,440),(3610,1330,440,162),(901,2786,142,440),(4922,431,440,39),(839,2,440,1),(4644,1240,440,117),(4396,832,440,83),(2979,5028,261,440),(209,577,159,440),(1704,5031,149,440),(3083,1217,440,174),(5198,2067,440,175),(2846,4934,254,440),(2984,3885,338,440),(1007,945,440,413),(3999,3818,440,420),(3936,3964,437,440),(2555,704,440,121),(1181,838,440,312),(2807,2169,440,340),(3921,5670,304,440),(1323,4230,138,440),(190,1682,50,440),(4328,2964,440,301),(1201,5654,93,440),(4450,222,440,22),(4327,2442,440,248),(5267,746,440,62),(5704,2140,440,165),(4247,3005,440,311),(1369,2914,207,440),(1826,4363,184,440),(4437,4119,440,408),(2701,5214,228,440),(1828,5024,160,440),(1599,1962,359,440),(3283,1858,440,249),(1638,4241,170,440),(4037,2913,440,317),(5989,238,440,17),(229,2289,44,440),(3869,2124,440,242),(1587,5674,123,440),(4958,2821,440,250),(3664,5924,272,440),(2864,2988,422,440),(660,1807,161,440),(837,1859,198,440),(3851,1612,440,184),(2767,1675,440,266),(3954,5113,340,440),(5000,16,440,2),(3928,5350,323,440),(2819,5269,235,440),(695,5412,57,440),(983,3183,136,440),(5829,1633,440,123),(3917,1463,440,164),(3555,5209,300,440),(2724,711,440,115),(5914,3243,440,241),(3795,3289,440,381),(696,5938,52,440),(1302,1393,411,440),(1041,226,440,96),(1239,4840,113,440),(3813,5373,312,440),(1198,5011,105,440),(4882,3886,440,350),(5385,2871,440,235),(1278,4495,125,440),(4492,1074,440,105),(176,117,176,117),(5951,5323,440,394),(842,4314,86,440),(1141,3554,141,440),(1596,1729,406,440),(230,2064,49,440),(1744,2400,320,440),(4106,1971,440,211),(4805,2671,440,245),(2125,4460,210,440),(3433,1074,440,138),(499,2899,76,440),(3754,5427,304,440),(4779,4234,440,390),(3446,4110,369,440),(1072,4357,108,440),(1244,4289,128,440),(4183,154,440,16),(3606,1501,440,183),(4986,33,440,3),(1228,1412,383,440),(1160,3879,132,440),(5072,5941,376,440),(986,4559,95,440),(506,2671,83,440),(5590,4247,440,334),(4348,4551,420,440),(3953,870,440,97),(4590,466,440,45),(2036,1568,440,339),(2269,346,440,67),(801,4160,85,440),(3705,4602,354,440),(229,520,194,440),(3632,2668,440,323),(5018,4142,440,363),(4966,4196,440,372),(1634,5675,127,440),(2271,3706,270,440),(4163,4369,419,440),(3917,4160,414,440),(2029,5728,156,440),(4287,2127,440,218),(4584,1660,440,159),(3667,1124,440,135),(3414,997,440,128),(3215,3622,391,440),(2589,595,440,101),(5499,1972,440,158),(3509,600,440,75),(1743,5485,140,440),(2481,1003,440,178),(1266,5867,95,440),(5272,5409,429,440),(3000,1172,440,172),(2074,1125,440,239),(3832,1799,440,207),(772,3263,104,440),(3992,1334,440,147),(5471,1833,440,147),(1323,5787,101,440),(3536,4224,368,440),(3309,2779,440,370),(3452,1604,440,204),(2922,2610,440,393),(756,5916,56,440),(2998,160,440,23),(2769,4539,268,440),(3758,3609,440,423),(5761,149,440,11),(3149,2716,440,379),(4239,5112,365,440),(2421,4197,254,440),(527,925,251,440),(1873,859,440,202),(689,2176,139,440),(2228,325,440,64),(1488,2216,295,440),(1062,3460,135,440),(5538,2119,440,168),(3326,1224,440,162),(4396,4218,440,422),(4675,4052,440,381),(5738,2680,440,206),(733,2287,141,440),(472,5638,37,440),(1502,3485,190,440),(594,2204,119,440),(138,5198,12,440),(726,2135,150,440),(687,4983,61,440),(1822,546,440,132),(2167,997,440,202),(3718,95,440,11),(2779,4531,270,440),(3423,2195,440,282),(5093,1059,440,91),(354,4317,36,440),(5813,1954,440,148),(897,1323,298,440),(2146,413,440,85),(1484,1653,395,440),(2556,5151,218,440),(2499,4351,253,440),(1687,2376,312,440),(3652,4097,392,440),(5507,1458,440,116),(2217,2843,343,440),(149,2052,32,440),(303,126,303,126),(152,4143,16,440),(4515,1553,440,151),(4213,3890,440,406),(2013,3663,242,440),(871,5393,71,440),(5326,3541,440,293),(5379,4056,440,332),(4473,3221,440,317),(4151,2522,440,267),(5634,1763,440,138),(1881,2808,295,440),(1628,5790,124,440),(5971,5210,440,384),(1145,3316,152,440),(2848,446,440,69),(1064,117,440,48),(580,5124,50,440),(2094,3529,261,440),(1338,454,440,149),(693,5450,56,440),(3121,4145,331,440),(5494,2310,440,185),(4906,1985,440,178),(5675,2401,440,186),(371,3764,43,440),(1519,1291,440,374),(2204,3653,265,440),(30,2157,6,440),(2984,2695,440,397),(4482,2651,440,260),(2003,283,440,62),(2536,1785,440,310),(2922,1499,440,226),(9,2748,1,440),(3127,688,440,97),(3889,2285,440,259),(4119,5375,337,440),(1647,2034,356,440),(4135,41,440,4),(745,2165,151,440),(736,1179,275,440),(3273,4808,300,440),(342,3228,47,440),(185,2455,33,440),(2493,5159,213,440),(1908,693,440,160),(4798,4336,440,398),(1272,5387,104,440),(5866,4888,440,367),(3191,2672,440,368),(5904,4049,440,302),(1225,2328,232,440),(5933,5069,440,376),(5270,1186,440,99),(359,5858,27,440),(4203,5140,360,440),(3517,5744,269,440),(4142,1142,440,121),(4291,4132,440,424),(4657,132,440,12),(5624,4785,440,374),(5827,5595,440,422),(5680,5267,440,408),(1884,698,440,163),(256,343,256,343),(1091,5220,92,440),(2955,860,440,128),(3086,3698,367,440),(4576,416,440,40),(5143,155,440,13),(5131,4354,440,373),(5577,2004,440,158),(4009,2161,440,237),(28,3744,3,440),(575,4121,61,440),(4385,754,440,76),(5401,4309,440,351),(542,3882,61,440),(2066,610,440,130),(2176,1924,440,389),(5975,1682,440,124),(1891,5325,156,440),(3772,4047,410,440),(3134,629,440,88),(3925,5601,308,440),(2354,383,440,72),(5055,5184,429,440),(5266,1625,440,136),(635,4913,57,440),(1208,2718,196,440),(2081,5338,172,440),(5677,2494,440,193),(5089,4652,440,402),(1094,103,440,41),(3952,497,440,55),(3980,2202,440,243),(5506,816,440,65),(5671,1784,440,138),(5536,4011,440,319),(2383,5808,181,440),(4232,2340,440,243),(3807,3817,439,440),(3821,971,440,112),(4499,1633,440,160),(2554,704,440,121),(3875,144,440,16),(2373,3760,278,440),(627,4151,66,440),(3682,2201,440,263),(3170,1719,440,239),(1727,612,440,156),(4764,740,440,68),(1162,4294,119,440),(2145,2946,320,440),(1087,4943,97,440),(5175,4168,440,354),(2291,924,440,177),(5762,2992,440,228)] {
            assert_eq!(thumbnail_size(w, h, 440).unwrap_or((w, h)), (tw, th), "{w}x{h}");
        }
    }
}
