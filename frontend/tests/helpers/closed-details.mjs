// Model native disclosures forced closed in static React markup. This deliberately
// does not claim to evaluate CSS visibility; browser checks cover that separately.
export function withDetailsClosed(html) {
  let depth = 0, summary = false, result = '';
  for (const token of html.match(/<[^>]+>|[^<]+/g) ?? []) {
    if (/^<details\b/.test(token)) {
      if (depth === 0) result += token.replace(/ open(?:="[^"]*")?/, '');
      depth++;
    } else if (token === '</details>') {
      depth--;
      if (depth === 0) result += token;
    } else if (/^<summary\b/.test(token) && depth === 1) {
      summary = true; result += token;
    } else if (token === '</summary>' && depth === 1) {
      result += token; summary = false;
    } else if (depth === 0 || (depth === 1 && summary)) result += token;
  }
  return result;
}
