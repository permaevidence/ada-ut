// Markdown -> Qt RichText for assistant chat bubbles (field test,
// 2026-08-28: Briglia's replies showed ** and backticks literally).
//
// Deliberately NOT a CommonMark renderer: chat replies use single
// newlines literally, and CommonMark's newline collapsing would mangle
// them. This converts only the subset Briglia actually emits — bold,
// *italic*, `inline code`, fenced code blocks, #headers, bullet lists,
// [links](url) — and passes everything else through HTML-escaped with
// newlines preserved as <br/>. User/command bubbles are never converted:
// what the user typed means exactly what it says.
//
// Single-underscore _italic_ is intentionally unsupported: identifiers
// like save_draft appear constantly in this app's conversations and
// would false-positive.
//
// Pure file, shared by ChatPage.qml and exercised under node by
// scripts/chat_selftest.py (this exact file).

function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Inline spans on one already-escaped line. Code spans are cut out first
// so the bold/italic/link passes can't touch their contents.
function inlineSpans(s) {
    var codes = [];
    s = s.replace(/`([^`]+)`/g, function(_m, c) {
        codes.push(c);
        return "\u0001" + (codes.length - 1) + "\u0001";
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    s = s.replace(/__([^_]+)__/g, "<b>$1</b>");
    // *italic*: content must not start or end with whitespace, and the
    // opener must not directly follow a word character (5*3*2 stays math).
    s = s.replace(/(^|[^*\w])\*([^*\s](?:[^*]*[^*\s])?)\*/g, "$1<i>$2</i>");
    s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
                  '<a href="$2">$1</a>');
    return s.replace(/\u0001(\d+)\u0001/g, function(_m, i) {
        return "<tt>" + codes[parseInt(i, 10)] + "</tt>";
    });
}

// RichText collapses runs of spaces — code indentation needs &nbsp;.
function hardenSpaces(s) {
    return s.replace(/ /g, "\u00a0");
}

function toRichText(md) {
    var lines = String(md || "").split("\n");
    var out = [];
    var inFence = false;
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (/^\s*```/.test(line)) {
            // Fence markers (with or without a language tag) render as
            // nothing; the block body renders monospace line by line.
            inFence = !inFence;
            continue;
        }
        if (inFence) {
            out.push("<tt>" + hardenSpaces(escapeHtml(line)) + "</tt>");
            continue;
        }
        var esc = escapeHtml(line);
        var header = esc.match(/^#{1,6}\s+(.*)$/);
        if (header) {
            out.push("<b>" + inlineSpans(header[1]) + "</b>");
            continue;
        }
        var bullet = esc.match(/^(\s*)[-*]\s+(.*)$/);
        if (bullet) {
            out.push(hardenSpaces(bullet[1]) + "• " + inlineSpans(bullet[2]));
            continue;
        }
        out.push(inlineSpans(esc));
    }
    // An unterminated fence is a model formatting slip — the monospace
    // lines above already rendered readably, nothing to repair.
    return out.join("<br/>");
}
