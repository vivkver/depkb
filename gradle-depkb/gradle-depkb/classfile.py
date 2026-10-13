"""Minimal, dependency-free Java class-file reader for depkb.

parse(data) -> dict with:
  name, super, interfaces            internal names (a/b/C)
  access                             class access flags
  refs      {internal name: abi}     every class the bytecode references; abi=1 when it appears in the
                                     public/protected API (supertypes, signatures of visible members)
  member_refs {(owner, name, desc)}  fields/methods this class calls or reads
  strings   [str]                    string literals (for reflection hints)
  fields / methods                   [(access, name, desc, signature, param_names)]
  signature, source_file
Also: render_class(info) -> Java-like API listing.
"""
import re
import struct

ACC_PUBLIC, ACC_PRIVATE, ACC_PROTECTED, ACC_STATIC = 0x1, 0x2, 0x4, 0x8
ACC_FINAL, ACC_BRIDGE, ACC_VARARGS, ACC_INTERFACE = 0x10, 0x40, 0x80, 0x200
ACC_ABSTRACT, ACC_SYNTHETIC, ACC_ANNOTATION, ACC_ENUM = 0x400, 0x1000, 0x2000, 0x4000
_CLS_IN_DESC = re.compile(r"L([A-Za-z_$][\w$/]*)(?=[;<])")


class _R:
    __slots__ = ("d", "p")

    def __init__(self, d):
        self.d, self.p = d, 0

    def u1(self):
        self.p += 1
        return self.d[self.p - 1]

    def u2(self):
        self.p += 2
        return struct.unpack_from(">H", self.d, self.p - 2)[0]

    def u4(self):
        self.p += 4
        return struct.unpack_from(">I", self.d, self.p - 4)[0]

    def skip(self, n):
        self.p += n


def _desc_classes(s):
    return _CLS_IN_DESC.findall(s or "")


def parse(data, members=True):
    r = _R(data)
    if r.u4() != 0xCAFEBABE:
        raise ValueError("not a class file")
    r.skip(2)
    major = r.u2()
    n = r.u2()
    cp = [None] * n
    i = 1
    while i < n:
        tag = r.u1()
        if tag == 1:
            ln = r.u2()
            cp[i] = ("U", bytes(data[r.p:r.p + ln]).decode("utf-8", "replace"))
            r.skip(ln)
        elif tag == 3:
            cp[i] = ("I", struct.unpack_from(">i", data, r.p)[0])
            r.skip(4)
        elif tag == 4:
            r.skip(4)
        elif tag in (5, 6):
            r.skip(8)
            i += 1
        elif tag == 7:
            cp[i] = ("C", r.u2())
        elif tag == 8:
            cp[i] = ("S", r.u2())
        elif tag in (9, 10, 11):
            cp[i] = ("R", r.u2(), r.u2())
        elif tag == 12:
            cp[i] = ("NT", r.u2(), r.u2())
        elif tag == 15:
            r.skip(3)
        elif tag == 16:
            cp[i] = ("MT", r.u2())
        elif tag in (17, 18):
            r.skip(4)
        elif tag in (19, 20):
            cp[i] = ("P", r.u2())  # Module / Package: name index
        else:
            raise ValueError(f"bad constant tag {tag}")
        i += 1

    def utf(k):
        e = cp[k] if 0 < k < n else None
        return e[1] if e and e[0] == "U" else None

    def cls(k):
        e = cp[k] if 0 < k < n else None
        return utf(e[1]) if e and e[0] == "C" else None

    def deprecation(end):
        """Parse a RuntimeVisibleAnnotations attribute; -> (forRemoval, since) if @Deprecated."""
        found = None

        def value():
            tag = chr(r.u1())
            if tag in "BCDFIJSZsc":
                return cp[r.u2()]
            if tag == "e":
                r.skip(4)
            elif tag == "@":
                annotation()
            elif tag == "[":
                for _ in range(r.u2()):
                    value()
            return None

        def annotation():
            nonlocal found
            t = utf(r.u2())
            ann_types.append(t)
            vals = {}
            for _ in range(r.u2()):
                k = utf(r.u2())
                vals[k] = value()
            if t == "Ljava/lang/Deprecated;":
                fr = vals.get("forRemoval")
                since = vals.get("since")
                found = (bool(fr and fr[0] == "I" and fr[1]), since[1] if since and since[0] == "U" else None)

        for _ in range(r.u2()):
            annotation()
        r.p = end
        return found

    deprecated = {}
    ann_types = []  # runtime-visible annotation types: needed at run time like any other reference
    access = r.u2()
    name = cls(r.u2())
    sup = cls(r.u2())
    interfaces = [cls(r.u2()) for _ in range(r.u2())]

    def read_members():
        out = []
        for _ in range(r.u2()):
            acc, nm, ds = r.u2(), utf(r.u2()), utf(r.u2())
            sig, pnames, lvt, exc = None, None, {}, []
            for _ in range(r.u2()):
                an, alen = utf(r.u2()), r.u4()
                end = r.p + alen
                if an == "RuntimeVisibleAnnotations":
                    d = deprecation(end)
                    if d:
                        deprecated[(nm, ds)] = d
                elif an == "Signature":
                    sig = utf(r.u2())
                elif an == "Exceptions":
                    exc = [cls(r.u2()) for _ in range(r.u2())]
                elif an == "MethodParameters" and members:
                    cnt = r.u1()
                    pnames = []
                    for _ in range(cnt):
                        pn = r.u2()
                        r.u2()
                        pnames.append(utf(pn) if pn else None)
                elif an == "Code" and members:
                    r.skip(4)
                    r.skip(r.u4())
                    r.skip(8 * r.u2())
                    for _ in range(r.u2()):
                        cn, clen = utf(r.u2()), r.u4()
                        cend = r.p + clen
                        if cn == "LocalVariableTable":
                            for _ in range(r.u2()):
                                start, _, vn, _, idx = r.u2(), r.u2(), r.u2(), r.u2(), r.u2()
                                if start == 0:
                                    lvt[idx] = utf(vn)
                        r.p = cend
                r.p = end
            out.append([acc, nm, ds, sig, pnames, lvt, exc])
        return out

    fields = read_members()
    methods = read_members()
    sig = src = None
    exports = None
    for _ in range(r.u2()):
        an, alen = utf(r.u2()), r.u4()
        end = r.p + alen
        if an == "Signature":
            sig = utf(r.u2())
        elif an == "SourceFile":
            src = utf(r.u2())
        elif an == "RuntimeVisibleAnnotations":
            d = deprecation(end)
            if d:
                deprecated[""] = d
        elif an == "Module":
            exports = []
            r.skip(6)
            for _ in range(r.u2()):  # requires
                r.skip(6)
            for _ in range(r.u2()):  # exports: only unqualified ones are public API
                pkg = cp[r.u2()]
                r.u2()
                to = r.u2()
                r.skip(2 * to)
                if not to and pkg and pkg[0] == "P":
                    exports.append(utf(pkg[1]))
        r.p = end

    # --- references
    refs = {}

    def add(c, abi=0):
        if not c:
            return
        c = c.lstrip("[")
        if c.startswith("L") and c.endswith(";"):
            c = c[1:-1]
        if len(c) <= 1 or c == name:  # primitives, self
            return
        refs[c] = max(refs.get(c, 0), abi)

    for e in cp:
        if e and e[0] == "C":
            v = utf(e[1]) or ""
            if v.startswith("["):
                for c in _desc_classes(v):
                    add(c)
            else:
                add(v)
    # descriptors that really reach the JVM: member/call-site types, signatures, visible annotations.
    # (A blanket scan of all UTF-8 constants would also pick up e.g. Kotlin @Metadata strings.)
    descs = [utf(e[2]) for e in cp if e and e[0] == "NT"] + [utf(e[1]) for e in cp if e and e[0] == "MT"]
    descs += [m[2] for m in fields + methods] + [m[3] for m in fields + methods if m[3]] + [sig] + ann_types
    for d in descs:
        for c in _desc_classes(d):
            add(c)
    member_refs = set()
    for e in cp:
        if e and e[0] == "R":
            owner = cls(e[1])
            nt = cp[e[2]]
            if owner and nt and nt[0] == "NT" and owner != name and not owner.startswith("["):
                member_refs.add((owner, utf(nt[1]), utf(nt[2])))
    # ABI: what a consumer of this class must see
    visible = access & ACC_PUBLIC
    if visible:
        for c in [sup] + interfaces + _desc_classes(sig):
            add(c, 1)
        for acc, _nm, ds, s, _p, _l, _e in fields + methods:
            if acc & (ACC_PUBLIC | ACC_PROTECTED) and not acc & ACC_SYNTHETIC:
                for c in _desc_classes(ds) + _desc_classes(s):
                    add(c, 1)
    strings = [utf(cp[k][1]) for k in range(n) if cp[k] and cp[k][0] == "S"]
    for m in methods:
        m[4] = m[4] or _names_from_lvt(m[0], m[2], m[5])
    return {"name": name, "super": sup, "interfaces": interfaces, "access": access, "refs": refs,
            "member_refs": member_refs, "strings": [s for s in strings if s],
            "fields": [tuple(f[:5]) for f in fields], "methods": [tuple(m[:5]) + (m[6],) for m in methods],
            "signature": sig, "source_file": src, "major": major, "deprecated": deprecated, "exports": exports,
            "int_fields": {f[1]: f[0] for f in fields}}


def _names_from_lvt(acc, desc, lvt):
    if not lvt:
        return None
    slot = 0 if acc & ACC_STATIC else 1
    names = []
    for t in _param_descs(desc):
        names.append(lvt.get(slot))
        slot += 2 if t in ("J", "D") else 1
    return names if any(names) else None


def _param_descs(desc):
    out, i = [], 1
    while i < len(desc) and desc[i] != ")":
        j = i
        while desc[j] == "[":
            j += 1
        j = desc.index(";", j) + 1 if desc[j] == "L" else j + 1
        out.append(desc[i:j])
        i = j
    return out


# ---------------------------------------------------------------- rendering

_PRIM = {"B": "byte", "C": "char", "D": "double", "F": "float", "I": "int", "J": "long",
         "S": "short", "Z": "boolean", "V": "void"}


class _Sig:
    """Parser for descriptors and generic signatures -> Java source-like types."""

    def __init__(self, s, short):
        self.s, self.i, self.short = s, 0, short

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def take(self):
        c = self.s[self.i]
        self.i += 1
        return c

    def ident(self, stops):
        j = self.i
        while self.i < len(self.s) and self.s[self.i] not in stops:
            self.i += 1
        return self.s[j:self.i]

    def type_params(self):
        if self.peek() != "<":
            return ""
        self.take()
        out = []
        while self.peek() != ">":
            nm = self.ident(":")
            bounds = []
            while self.peek() == ":":
                self.take()
                if self.peek() in ("L", "T", "["):
                    b = self.ref()
                    if b != "Object":
                        bounds.append(b)
            out.append(nm + (" extends " + " & ".join(bounds) if bounds else ""))
        self.take()
        return "<" + ", ".join(out) + ">"

    def type(self):
        c = self.peek()
        if c in _PRIM:
            self.take()
            return _PRIM[c]
        return self.ref()

    def ref(self):
        c = self.take()
        if c == "[":
            return self.type() + "[]"
        if c == "T":
            nm = self.ident(";")
            self.take()
            return nm
        # L pkg/Outer<Args>.Inner<Args>;
        parts = []
        while True:
            nm = self.ident("<.;")
            args = ""
            if self.peek() == "<":
                self.take()
                a = []
                while self.peek() != ">":
                    k = self.peek()
                    if k == "*":
                        self.take()
                        a.append("?")
                    elif k in "+-":
                        self.take()
                        a.append(("? extends " if k == "+" else "? super ") + self.ref())
                    else:
                        a.append(self.ref())
                self.take()
                args = "<" + ", ".join(a) + ">"
            parts.append((nm, args))
            if self.take() == ";":
                break
        name = self.short(parts[0][0]) + parts[0][1]
        for nm, args in parts[1:]:
            name += "." + nm + args
        return name


def _shortener(own_pkg):
    """internal name a/b/Outer$Inner -> 'Outer.Inner' for java.lang and the own package, else FQN."""
    def short(internal):
        pkg, _, simple = internal.rpartition("/")
        pkg, simple = pkg.replace("/", "."), simple.replace("$", ".")
        return simple if pkg in ("java.lang", own_pkg) else (f"{pkg}.{simple}" if pkg else simple)
    return short


def _mods(acc, is_iface_member=False):
    m = []
    if acc & ACC_PUBLIC and not is_iface_member:
        m.append("public")
    if acc & ACC_PROTECTED:
        m.append("protected")
    if acc & ACC_STATIC:
        m.append("static")
    if acc & ACC_FINAL:
        m.append("final")
    if acc & ACC_ABSTRACT and not is_iface_member:
        m.append("abstract")
    return " ".join(m)


def render_class(info, include_protected=True, docs=None):
    """Java-like listing of the visible API. docs: {member name or '': first sentence}."""
    docs = docs or {}
    fqn = info["name"].replace("/", ".")
    pkg = fqn.rpartition(".")[0]
    short = _shortener(pkg)
    simple = fqn.rpartition(".")[2].replace("$", ".")
    acc = info["access"]
    kind = ("@interface" if acc & ACC_ANNOTATION else "interface" if acc & ACC_INTERFACE
            else "enum" if acc & ACC_ENUM else "record" if info["super"] == "java/lang/Record" else "class")
    head = _mods(acc & ~(ACC_ABSTRACT | ACC_FINAL if acc & (ACC_INTERFACE | ACC_ENUM) else 0) & ~ACC_SYNTHETIC)
    head = (head + " " if head else "") + kind + " " + simple
    if info["signature"]:
        p = _Sig(info["signature"], short)
        head += p.type_params()
        sup = p.ref()
        ifs = []
        while p.peek():
            ifs.append(p.ref())
    else:
        sup = short(info["super"]) if info["super"] else None
        ifs = [short(x) for x in info["interfaces"]]
    if sup and sup not in ("Object", "Enum", "Record") and not sup.startswith(("Enum<", "Record")) and kind == "class":
        head += " extends " + sup
    if ifs and kind != "@interface":
        head += (" extends " if kind == "interface" else " implements ") + ", ".join(ifs)
    lines = [f"package {pkg};", head + " {"]
    if docs.get(""):
        lines.insert(1, f"/** {docs['']} */")
    vis = ACC_PUBLIC | (ACC_PROTECTED if include_protected else 0)
    iface = bool(acc & ACC_INTERFACE)
    consts = [nm for facc, nm, *_ in info["fields"] if facc & ACC_ENUM]
    if consts:
        lines.append("  " + ", ".join(consts) + ";")
    for facc, nm, ds, sg, _ in info["fields"]:
        if not facc & vis or facc & (ACC_SYNTHETIC | ACC_ENUM):
            continue
        t = _Sig(sg or ds, short).type()
        lines.append(f"  {_mods(facc, iface)} {t} {nm};".replace("  ", "  ", 1))
    for macc, nm, ds, sg, pnames, exc in info["methods"]:
        if not macc & vis or macc & (ACC_SYNTHETIC | ACC_BRIDGE) or nm == "<clinit>":
            continue
        if acc & ACC_ENUM and nm in ("values", "valueOf"):
            continue
        p = _Sig(sg or ds, short)
        tp = p.type_params()
        p.take()  # (
        params = []
        while p.peek() != ")":
            params.append(p.type())
        p.take()
        ret = p.type()
        throws = []
        while p.peek() == "^":
            p.take()
            throws.append(p.ref())
        if macc & ACC_VARARGS and params and params[-1].endswith("[]"):
            params[-1] = params[-1][:-2] + "..."
        names = pnames or []
        # signatures of inner-class/enum constructors omit synthetic params: align from the right
        if names and len(names) > len(params):
            names = names[len(names) - len(params):]
        ps = ", ".join(f"{t} {names[i]}" if i < len(names) and names[i] else t for i, t in enumerate(params))
        mods = _mods(macc, iface)
        if iface and macc & ACC_STATIC == 0 and macc & ACC_ABSTRACT == 0:
            mods = (mods + " default").strip()
        sigtxt = (f"{tp} " if tp else "") + (f"{simple.rpartition('.')[2]}({ps})" if nm == "<init>"
                                             else f"{ret} {nm}({ps})")
        if not throws and exc:
            throws = [short(x) for x in exc if x]
        if throws:
            sigtxt += " throws " + ", ".join(throws)
        doc = docs.get(nm)
        lines.append(f"  {(mods + ' ') if mods else ''}{sigtxt};" + (f"  // {doc}" if doc else ""))
    lines.append("}")
    return "\n".join(lines)
