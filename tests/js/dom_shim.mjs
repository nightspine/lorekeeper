// A minimal XHTML-to-DOM parser for Node tests (Node has no DOMParser). It supports what the
// client uses: nodeType, nodeName, localName, parentNode, childNodes, children, getAttribute, id,
// data, textContent, documentElement and getElementById.
//
// Limits: no namespaces (a prefix stays in nodeName, localName drops it), no DTD or external
// entities, only these named entities (amp lt gt quot apos nbsp ndash mdash hellip lsquo rsquo
// ldquo rdquo plus all numeric ones), no HTML error recovery beyond void elements and closing a
// mismatched end tag by popping to the matching open element. Fine for well-formed XHTML.

const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", ndash: "–",
  mdash: "—", hellip: "…", lsquo: "‘", rsquo: "’", ldquo: "“", rdquo: "”" };
const VOID = new Set(["br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr"]);
const RAW = new Set(["script", "style"]);

const decode = (s) => s.replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) => {
  if (e[0] === "#") return String.fromCodePoint(e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
  return ENTITIES[e] ?? m;
});

class Node {
  constructor(nodeType, nodeName) {
    this.nodeType = nodeType; this.nodeName = nodeName; this.parentNode = null; this.childNodes = [];
  }
  appendChild(c) { c.parentNode = this; this.childNodes.push(c); return c; }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get textContent() {
    return this.childNodes.filter((c) => c.nodeType === 1 || c.nodeType === 3 || c.nodeType === 4)
      .map((c) => (c.nodeType === 1 ? c.textContent : c.data)).join("");
  }
}

class Text extends Node {
  constructor(data, type = 3) { super(type, type === 3 ? "#text" : "#cdata-section"); this.data = data; this.nodeValue = data; }
  get textContent() { return this.data; }
}

class Element extends Node {
  constructor(name, attrs) {
    super(1, name); this.attrs = attrs; this.localName = name.includes(":") ? name.split(":")[1] : name;
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  get id() { return this.attrs.id ?? ""; }
}

class Document extends Node {
  constructor() { super(9, "#document"); }
  get documentElement() { return this.children[0] ?? null; }
  getElementById(id) {
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (c.nodeType !== 1) continue;
        if (c.attrs.id === id) return c;
        const r = walk(c); if (r) return r;
      }
      return null;
    };
    return walk(this);
  }
}

export function parseXhtml(src) {
  const doc = new Document();
  let cur = doc, i = 0;
  const text = (s) => { if (s && cur !== doc) cur.appendChild(new Text(decode(s))); };
  while (i < src.length) {
    const lt = src.indexOf("<", i);
    if (lt < 0) { text(src.slice(i)); break; }
    text(src.slice(i, lt));
    if (src.startsWith("<!--", lt)) { i = src.indexOf("-->", lt) + 3; continue; }
    if (src.startsWith("<![CDATA[", lt)) {
      const end = src.indexOf("]]>", lt);
      if (cur !== doc) cur.appendChild(new Text(src.slice(lt + 9, end), 4));
      i = end + 3; continue;
    }
    if (src.startsWith("<?", lt)) { i = src.indexOf("?>", lt) + 2; continue; }
    if (src.startsWith("<!", lt)) { i = src.indexOf(">", lt) + 1; continue; }
    const gt = (() => {                      // end of tag, honouring quoted attribute values
      let q = null;
      for (let j = lt + 1; j < src.length; j++) {
        const ch = src[j];
        if (q) { if (ch === q) q = null; } else if (ch === '"' || ch === "'") q = ch; else if (ch === ">") return j;
      }
      return src.length - 1;
    })();
    const tag = src.slice(lt + 1, gt);
    i = gt + 1;
    if (tag[0] === "/") {
      const name = tag.slice(1).trim();
      for (let n = cur; n !== doc; n = n.parentNode) if (n.nodeName === name) { cur = n.parentNode; break; }
      continue;
    }
    const m = /^([^\s/>]+)([\s\S]*?)(\/?)$/.exec(tag);
    const attrs = {};
    for (const a of m[2].matchAll(/([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) attrs[a[1]] = decode(a[2] ?? a[3]);
    const el = new Element(m[1], attrs);
    cur.appendChild(el);
    const lname = el.localName.toLowerCase();
    if (m[3] || VOID.has(lname)) continue;
    if (RAW.has(lname)) {
      const close = src.toLowerCase().indexOf(`</${lname}`, i);
      el.appendChild(new Text(src.slice(i, close < 0 ? src.length : close)));
      i = close < 0 ? src.length : src.indexOf(">", close) + 1;
      continue;
    }
    cur = el;
  }
  return doc;
}
