/**
 * Minimal ES2022 DOM & Browser harness for Node unit tests of Bellhop UI modules.
 *
 * Parses rendered HTML fragments cleanly so `querySelector`, `querySelectorAll`,
 * `closest`, `contains`, `dataset`, `classList`, `replaceWith`, and focus state
 * operate directly on the real markup produced by `render.js`.
 */

function decodeHtmlAttr(s) {
  return String(s)
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
}

function parseStyleString(raw) {
  const obj = {};
  for (const part of String(raw || "").split(";")) {
    const idx = part.indexOf(":");
    if (idx === -1) continue;
    const key = part.slice(0, idx).trim();
    const val = part.slice(idx + 1).trim();
    if (key) obj[key] = val;
  }
  return obj;
}

function kebabToCamel(s) {
  return s.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

export class FakeElement {
  constructor(tagName = "div", ownerDocument = null) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.id = "";
    this.parentNode = null;
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.muted = false;
    this.paused = true;
    this.src = "";
    this.scrollLeft = 0;
    this.scrollTop = 0;
    this._textContent = "";
    this.title = "";
    this.type = "";
    this.tabIndex = -1;
    this.style = { width: "", left: "", overflow: "", cssText: "" };
    this.dataset = {};
    this._attrs = new Map();
    this._classes = new Set();
    this._listeners = new Map();
    this._innerHTML = "";
    this.classList = {
      add: (...items) => {
        for (const it of items) {
          if (it) this._classes.add(it);
        }
      },
      remove: (...items) => {
        for (const it of items) this._classes.delete(it);
      },
      toggle: (cls, force) => {
        if (force === undefined) {
          if (this._classes.has(cls)) {
            this._classes.delete(cls);
            return false;
          }
          this._classes.add(cls);
          return true;
        }
        if (force) {
          this._classes.add(cls);
          return true;
        }
        this._classes.delete(cls);
        return false;
      },
      contains: (cls) => this._classes.has(cls),
    };
  }

  get name() {
    return this._attrs.get("name") || "";
  }

  set name(val) {
    this._attrs.set("name", String(val || ""));
  }

  get className() {
    return Array.from(this._classes).join(" ");
  }

  set className(val) {
    this._classes = new Set(String(val || "").split(/\s+/).filter(Boolean));
  }

  get textContent() {
    if (this._textContent) return this._textContent;
    return this.children.map((c) => c.textContent).join("");
  }

  set textContent(val) {
    this.children = [];
    this._innerHTML = "";
    this._textContent = val == null ? "" : String(val);
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(html) {
    this._innerHTML = String(html || "");
    this._textContent = "";
    this.children = [];
    if (this._innerHTML) {
      parseHtmlInto(this._innerHTML, this, this.ownerDocument);
    }
  }

  get isConnected() {
    let cur = this;
    while (cur) {
      if (this.ownerDocument && cur === this.ownerDocument.body) return true;
      if (!this.ownerDocument && cur.parentNode === null) return cur === this;
      cur = cur.parentNode;
    }
    return false;
  }

  appendChild(child) {
    child.parentNode = this;
    child.ownerDocument = this.ownerDocument;
    this.children.push(child);
    return child;
  }

  replaceWith(replacement) {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    if (idx !== -1) {
      replacement.parentNode = this.parentNode;
      replacement.ownerDocument = this.ownerDocument;
      siblings.splice(idx, 1, replacement);
    }
    this.parentNode = null;
  }

  setAttribute(k, v) {
    const key = String(k);
    const val = String(v);
    this._attrs.set(key, val);
    if (key === "id") this.id = val;
    else if (key === "class") this.className = val;
    else if (key === "hidden") this.hidden = true;
    else if (key === "disabled") this.disabled = true;
    else if (key === "title") this.title = decodeHtmlAttr(val);
    else if (key === "type") this.type = val;
    else if (key === "name") this.name = val;
    else if (key === "src") this.src = decodeHtmlAttr(val);
    else if (key === "tabindex") this.tabIndex = Number(val);
    else if (key === "style") Object.assign(this.style, parseStyleString(val));
    else if (key.startsWith("data-")) {
      this.dataset[kebabToCamel(key.slice(5))] = decodeHtmlAttr(val);
    }
  }

  getAttribute(k) {
    const key = String(k);
    if (key === "id" && this.id) return this.id;
    if (key === "class") return this.className;
    if (key === "hidden") return this.hidden ? "" : null;
    if (key === "disabled") return this.disabled ? "" : null;
    if (key === "type" && this.type) return this.type;
    if (key === "tabindex" && this._attrs.has("tabindex")) {
      return String(this.tabIndex);
    }
    return this._attrs.has(key) ? this._attrs.get(key) : null;
  }

  removeAttribute(k) {
    this._attrs.delete(k);
    if (k === "hidden") this.hidden = false;
    else if (k === "disabled") this.disabled = false;
    else if (k === "src") this.src = "";
  }

  hasAttribute(k) {
    return this.getAttribute(k) !== null;
  }

  contains(node) {
    let cur = node;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentNode;
    }
    return false;
  }

  focus() {
    if (this.ownerDocument) {
      this.ownerDocument.activeElement = this;
    }
  }

  pause() {
    this.paused = true;
  }

  load() {}

  play() {
    this.paused = false;
    return Promise.resolve();
  }

  canPlayType() {
    return "";
  }

  addEventListener(type, fn, capture = false) {
    const key = `${type}:${Boolean(capture)}`;
    if (!this._listeners.has(key)) this._listeners.set(key, []);
    this._listeners.get(key).push(fn);
  }

  removeEventListener(type, fn, capture = false) {
    const key = `${type}:${Boolean(capture)}`;
    const list = this._listeners.get(key);
    if (!list) return;
    this._listeners.set(
      key,
      list.filter((f) => f !== fn),
    );
  }

  async dispatch(type, ev = {}, capture = false) {
    const event = {
      target: this,
      preventDefault() {
        this.defaultPrevented = true;
      },
      defaultPrevented: false,
      ...ev,
    };
    const key = `${type}:${Boolean(capture)}`;
    for (const fn of [...(this._listeners.get(key) || [])]) {
      await fn(event);
    }
    return event;
  }

  matches(selector) {
    const alternatives = selector
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    return alternatives.some((sel) => matchSelectorChain(this, sel));
  }

  closest(selector) {
    let cur = this;
    while (cur && cur.tagName) {
      if (cur.matches(selector)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }

  querySelectorAll(selector) {
    const raw = String(selector).trim();
    const parts = raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const results = [];
    const seen = new Set();

    const visit = (node) => {
      for (const child of node.children) {
        for (const part of parts) {
          if (part.startsWith(":scope >")) {
            const sub = part.slice(":scope >".length).trim();
            if (child.parentNode === this && matchSimpleSelector(child, sub)) {
              if (!seen.has(child)) {
                seen.add(child);
                results.push(child);
              }
            }
          } else if (matchSelectorChain(child, part, this)) {
            if (!seen.has(child)) {
              seen.add(child);
              results.push(child);
            }
          }
        }
        visit(child);
      }
    };
    visit(this);
    return results;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

export class FakeImageElement extends FakeElement {
  constructor(ownerDocument = null) {
    super("img", ownerDocument);
  }
}

function matchSelectorChain(el, chain, scopeRoot = null) {
  const tokens = chain.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return false;
  if (!matchSimpleSelector(el, tokens[tokens.length - 1])) return false;
  let cur = el.parentNode;
  for (let i = tokens.length - 2; i >= 0; i -= 1) {
    const tok = tokens[i];
    if (tok === ">") continue;
    let matched = false;
    while (cur && cur.tagName) {
      if (matchSimpleSelector(cur, tok)) {
        matched = true;
        cur = cur.parentNode;
        break;
      }
      if (cur === scopeRoot) break;
      cur = cur.parentNode;
    }
    if (!matched) return false;
  }
  return true;
}

function matchSimpleSelector(el, sel) {
  let rem = sel.trim();
  if (!rem) return false;
  // Support basic tag, #id, .class, [attr], [attr='val'], [attr="val"], :not(...)
  const notClauses = [];
  rem = rem.replace(/:not\(([^)]+)\)/g, (_, inner) => {
    notClauses.push(inner.trim());
    return "";
  });

  const attrClauses = [];
  rem = rem.replace(/\[([^\]]+)\]/g, (_, inner) => {
    attrClauses.push(inner.trim());
    return "";
  });

  const tagMatch = rem.match(/^[A-Za-z][A-Za-z0-9-]*/);
  if (tagMatch) {
    if (el.tagName !== tagMatch[0].toUpperCase()) return false;
    rem = rem.slice(tagMatch[0].length);
  }

  const classMatches = rem.match(/\.[A-Za-z0-9_-]+/g) || [];
  for (const cls of classMatches) {
    if (!el.classList.contains(cls.slice(1))) return false;
  }
  const idMatches = rem.match(/#[A-Za-z0-9_-]+/g) || [];
  for (const idm of idMatches) {
    if (el.id !== idm.slice(1)) return false;
  }

  for (const ac of attrClauses) {
    const eqIdx = ac.indexOf("=");
    if (eqIdx === -1) {
      if (!el.hasAttribute(ac)) return false;
    } else {
      const k = ac.slice(0, eqIdx).trim();
      let v = ac.slice(eqIdx + 1).trim();
      if (
        (v.startsWith('"') && v.endsWith('"')) ||
        (v.startsWith("'") && v.endsWith("'"))
      ) {
        v = v.slice(1, -1);
      }
      if (el.getAttribute(k) !== v) return false;
    }
  }

  for (const nc of notClauses) {
    if (matchSimpleSelector(el, nc)) return false;
  }
  return true;
}

const VOID_TAGS = new Set(["img", "input", "br", "hr", "meta", "link"]);

function parseHtmlInto(html, rootEl, ownerDoc) {
  const stack = [rootEl];
  const tokenRe = /<\/([A-Za-z0-9-]+)\s*>|<([A-Za-z0-9-]+)((?:\s+[^\s=/>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = tokenRe.exec(html)) !== null) {
    const current = stack[stack.length - 1];
    if (m[1]) {
      const closeTag = m[1].toUpperCase();
      for (let i = stack.length - 1; i >= 1; i -= 1) {
        if (stack[i].tagName === closeTag) {
          stack.length = i;
          break;
        }
      }
    } else if (m[2]) {
      const tag = m[2].toLowerCase();
      const el =
        tag === "img"
          ? new FakeImageElement(ownerDoc)
          : new FakeElement(tag, ownerDoc);
      const attrStr = m[3] || "";
      const attrRe = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
      let am;
      while ((am = attrRe.exec(attrStr)) !== null) {
        const k = am[1];
        const v = am[2] ?? am[3] ?? am[4] ?? "";
        el.setAttribute(k, v);
      }
      current.appendChild(el);
      if (m[4] !== "/" && !VOID_TAGS.has(tag)) {
        stack.push(el);
      }
    } else if (m[5]) {
      current._textContent += decodeHtmlAttr(m[5]);
    }
  }
}

export class FakeDocument {
  constructor() {
    this.documentElement = new FakeElement("html", this);
    this.body = new FakeElement("body", this);
    this.documentElement.appendChild(this.body);
    this.activeElement = null;
    this.visibilityState = "visible";
    this.title = "";
    this._byId = new Map();
    this._listeners = new Map();
  }

  register(el) {
    if (el.id) this._byId.set(el.id, el);
    return el;
  }

  createElement(tag) {
    return tag.toLowerCase() === "img"
      ? new FakeImageElement(this)
      : new FakeElement(tag, this);
  }

  getElementById(id) {
    return this._byId.get(id) ?? this.body.querySelector(`#${id}`) ?? null;
  }

  querySelectorAll(selector) {
    return this.body.querySelectorAll(selector);
  }

  querySelector(selector) {
    return this.body.querySelector(selector);
  }

  addEventListener(type, fn, capture = false) {
    const key = `${type}:${Boolean(capture)}`;
    if (!this._listeners.has(key)) this._listeners.set(key, []);
    this._listeners.get(key).push(fn);
  }

  removeEventListener(type, fn, capture = false) {
    const key = `${type}:${Boolean(capture)}`;
    const list = this._listeners.get(key);
    if (!list) return;
    this._listeners.set(
      key,
      list.filter((f) => f !== fn),
    );
  }

  async dispatch(type, ev = {}, capture = false) {
    const event = {
      preventDefault() {
        this.defaultPrevented = true;
      },
      defaultPrevented: false,
      ...ev,
    };
    const key = `${type}:${Boolean(capture)}`;
    for (const fn of [...(this._listeners.get(key) || [])]) {
      await fn(event);
    }
    return event;
  }
}
