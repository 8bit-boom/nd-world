"""Writes rust/nd-gateway/src/sheet_import_cases.json: HTML pages with the text app.rendering.html_to_sheet_text makes of each, which the Rust
reader (sheet_import.rs) is tested against.   usage: python scripts/gen_sheet_html_cases.py"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", "/tmp/sheet_cases.db")
from app.rendering import html_to_sheet_text  # noqa: E402

PAGES = [
    "<html><head><title> Ana &amp; Co </title><style>p{color:red}</style></head><body><h1>Ana Vos</h1><p>A ranger. <b>Bold</b> text.</p></body></html>",
    "<form><label>Name</label><input name='pname' value=' Ana '><input type='hidden' name='h' value='x'><input type='password' name='pw' value='secret'>"
    "<input type='checkbox' name='stealth' checked> Stealth <input type='checkbox' name='lore'> Lore <input type='radio' name='side' checked></form>",
    "<select name='class'><option>Bard</option><option selected>Ranger</option><option>Sage</option></select><select id='x'><option>One</option><option>Two</option></select>",
    "<textarea name='bio'>\n  Born in the silt.\n  Lost a brother.\n\n</textarea><textarea></textarea><textarea name='e'>   </textarea>",
    "<table><tr><th>Skill</th><th>Rank</th></tr><tr><td>Climb</td><td>3</td></tr><tr><td>Sneak</td><td>4</td></tr></table>",
    "<ul><li>Rope</li><li>Knife <i>(sharp)</i></li></ul><ol><li>One</li><li>Two</li></ol><hr><h3>Gear</h3><p>Lantern</p>",
    "<div>One<br>Two<br/>Three</div><div>Four</div><span>a</span><span> b </span>",
    "<script>var x = '<b>no</b>';</script><p>kept</p><noscript>gone</noscript><button>click</button><svg><text>x</text></svg>",
    "<!-- a comment --><p>after &lt;comment&gt; &#169; &copy; &nbsp;nbsp &unknown; &#x41;</p><!DOCTYPE html><?pi x?>",
    "<pre>  keep   spaces\n  and lines</pre><p>after   pre</p>",
    "<input name='n' value=''><input name='age' value='42'><input type='number' aria-label='Level' name='lvl' value='5'><input placeholder='Alias' value='Fox'>",
    "<p>Unclosed <b>bold <i>italic<p>next paragraph<li>item one<li>item two",
    "<h2>Stats</h2><table><tr><td>STR</td><td><input value='9'></td><td>DEX</td><td><input value='14'></td></tr></table>",
    "<p>line one</p>\r\n\r\n\r\n\r\n<p>line two</p>\x0c<p>three</p>",
    "<a href='x' title=\"t\">link text</a> <img src='a.png' alt='pic'> <p class=c id=d data-x>unquoted attrs</p>",
    "plain text only, no tags & an ampersand < not a tag",
    "<title>Only a title</title>",
    "<details><summary>Sum</summary>Body</details><blockquote>Quote</blockquote><dl><dt>Term</dt><dd>Def</dd></dl>",
    "<p>" + "word " * 60000 + "</p>",
    "<DIV>Upper</DIV><P>Case</P><INPUT NAME='Up' VALUE='V'>",
    "<style>.a>b{x:1}</style><p>a < b and c > d</p><p>x<y</p>",
]

if __name__ == "__main__":
    cases = [{"html": h, "text": html_to_sheet_text(h)} for h in PAGES]
    out = ROOT / "rust" / "nd-gateway" / "src" / "sheet_import_cases.json"
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("wrote", out, len(cases))
