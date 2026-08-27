"""Minimal readers for a mysqldump .sql file and PHP-serialized values.

The WordPress backup we import from is an 80MB mysqldump. Loading it into a
real MySQL server just to read six tables isn't worth it, so this module
streams the file and parses only the `INSERT INTO` statements for the tables
we care about. Both parsers are deliberately narrow: they handle exactly the
shapes mysqldump and PHP's `serialize()` emit, and raise on anything else
rather than guessing.
"""

import re


def extract_tables(sql_path, tables):
    """Stream `sql_path` and return {table: raw INSERT statement text}.

    mysqldump writes one `INSERT INTO `t` VALUES` per table with the tuples
    following on subsequent lines, so a statement is accumulated until the
    line that ends with `;`.
    """
    wanted = set(tables)
    chunks = {t: [] for t in wanted}
    current = None
    with open(sql_path, 'rb') as handle:
        for line in handle:
            if current is None:
                if not line.startswith(b'INSERT INTO `'):
                    continue
                end = line.index(b'`', 13)
                name = line[13:end].decode()
                if name not in wanted:
                    continue
                current = name
            chunks[current].append(line)
            if line.rstrip().endswith(b';'):
                current = None
    return {
        t: b''.join(parts).decode('utf-8', errors='replace')
        for t, parts in chunks.items()
    }


_INSERT_RE = re.compile(r'INSERT INTO `[^`]+` VALUES\s*', re.S)
_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0', 'b': '\b', 'Z': '\x1a'}


def parse_rows(sql_text):
    """Parse `INSERT INTO x VALUES (..),(..);` into a list of tuples.

    Values come back as `str` or `None`; numeric columns are left as strings
    because every caller either compares them as identifiers or converts
    explicitly.
    """
    rows = []
    length = len(sql_text)
    position = 0
    while True:
        match = _INSERT_RE.search(sql_text, position)
        if not match:
            return rows
        position = match.end()
        while position < length:
            while position < length and sql_text[position] in ' \n\r\t,':
                position += 1
            if position >= length or sql_text[position] == ';':
                position += 1
                break
            if sql_text[position] != '(':
                raise ValueError(f'unexpected token at {position}: {sql_text[position]!r}')
            position += 1
            row = []
            while True:
                while sql_text[position] in ' \n\r\t':
                    position += 1
                if sql_text[position] == "'":
                    position += 1
                    buf = []
                    while True:
                        char = sql_text[position]
                        if char == '\\':
                            nxt = sql_text[position + 1]
                            buf.append(_ESCAPES.get(nxt, nxt))
                            position += 2
                        elif char == "'":
                            # Doubled '' is an escaped quote, a lone one ends the string.
                            if sql_text[position + 1:position + 2] == "'":
                                buf.append("'")
                                position += 2
                            else:
                                position += 1
                                break
                        else:
                            buf.append(char)
                            position += 1
                    row.append(''.join(buf))
                else:
                    end = position
                    while end < length and sql_text[end] not in ',)':
                        end += 1
                    token = sql_text[position:end].strip()
                    row.append(None if token.upper() == 'NULL' else token)
                    position = end
                while sql_text[position] in ' \n\r\t':
                    position += 1
                if sql_text[position] == ',':
                    position += 1
                    continue
                if sql_text[position] == ')':
                    position += 1
                    break
            rows.append(tuple(row))


def php_unserialize(text):
    """Decode a PHP `serialize()` string (arrays, strings, ints, floats, bools, null).

    FluentCommunity keeps post/profile extras in a serialized `meta` column,
    which is where post images and poll options live. Returns None for empty
    or unparseable input -- meta is decoration, never worth failing on.
    """
    if not text:
        return None
    try:
        value, _ = _php_value(text, 0)
        return value
    except (IndexError, ValueError):
        return None


def _php_value(text, i):
    kind = text[i]
    if kind == 'N':
        return None, i + 2  # N;
    if kind in 'ibd':
        end = text.index(';', i)
        raw = text[i + 2:end]
        if kind == 'i':
            return int(raw), end + 1
        if kind == 'b':
            return raw == '1', end + 1
        return float(raw), end + 1
    if kind == 's':
        colon = text.index(':', i + 2)
        size = int(text[i + 2:colon])
        start = colon + 2  # skip the opening quote
        # `size` is a byte length, but we hold a str -- walk out from the
        # character count until the encoded slice matches, so multi-byte
        # Greek text doesn't truncate mid-string.
        end = start + size
        while len(text[start:end].encode('utf-8')) > size:
            end -= 1
        return text[start:end], end + 2  # skip closing quote and ;
    if kind == 'a':
        colon = text.index(':', i + 2)
        count = int(text[i + 2:colon])
        i = colon + 2  # skip {
        out = {}
        for _ in range(count):
            key, i = _php_value(text, i)
            val, i = _php_value(text, i)
            out[key] = val
        return out, i + 1  # skip }
    raise ValueError(f'unsupported php type {kind!r} at {i}')
