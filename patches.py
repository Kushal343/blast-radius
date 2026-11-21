"""Read unified diffs and apply text hunks to the pinned source in memory."""

import re
import shlex
from dataclasses import dataclass, field


HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


@dataclass
class Hunk:
    start: int
    size: int
    new_size: int
    lines: list[str] = field(default_factory=list)


@dataclass
class Change:
    before: str | None
    after: str | None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self):
        return self.before or self.after

    def apply(self, source):
        original = source.removesuffix('\n').split('\n') if source else []
        result = []
        removed = set()
        added = set()
        inserts = {}
        cursor = 0
        for hunk in self.hunks:
            first = hunk.start if hunk.size == 0 else hunk.start - 1
            if not cursor <= first <= len(original):
                raise ValueError('hunk offset outside original source')
            result.extend(original[cursor:first])
            cursor = first
            old_count = new_count = 0
            replacing = False
            for line in hunk.lines:
                kind, content = line[0], line[1:]
                if kind == '+':
                    result.append(content)
                    added.add(len(result))
                    if not replacing:
                        inserts[len(result)] = cursor
                    new_count += 1
                    continue
                if cursor >= len(original) or original[cursor] != content:
                    raise ValueError('hunk does not match pinned source')
                old_count += 1
                cursor += 1
                if kind == '-':
                    removed.add(cursor)
                    replacing = True
                else:
                    result.append(content)
                    new_count += 1
                    replacing = False
            if (old_count, new_count) != (hunk.size, hunk.new_size):
                raise ValueError('incomplete hunk')
        result.extend(original[cursor:])
        return '\n'.join(result) + '\n', removed, added, inserts


def path_from_header(value, prefix):
    value = value.split('\t', 1)[0]
    if value.startswith('"'):
        value = shlex.split(value)[0]
    if value == '/dev/null':
        return None
    value = value.removeprefix(prefix)
    if value.startswith('/') or '..' in value.split('/'):
        raise ValueError('invalid diff path')
    return value


def read_patch(text):
    changes = []
    change = active = None
    old_left = new_left = 0
    for line in text.split('\n'):
        if active is not None and (old_left or new_left):
            if line.startswith('\\ '):
                continue
            if not line or line[0] not in ' +-':
                raise ValueError('invalid hunk body')
            active.lines.append(line)
            old_left -= line[0] != '+'
            new_left -= line[0] != '-'
            if old_left < 0 or new_left < 0:
                raise ValueError('incorrect hunk size')
        elif line.startswith('diff --git '):
            before, after = shlex.split(line[11:])
            change = Change(path_from_header(before, 'a/'), path_from_header(after, 'b/'))
            changes.append(change)
        elif line.startswith('--- '):
            if change is None or change.hunks:
                change = Change(None, None)
                changes.append(change)
            change.before = path_from_header(line[4:], 'a/')
        elif line.startswith('+++ ') and change is not None:
            change.after = path_from_header(line[4:], 'b/')
        elif match := HEADER.match(line):
            if change is None:
                raise ValueError('hunk without file headers')
            first, old_count, _, new_count = match.groups()
            old_left = int(old_count or 1)
            new_left = int(new_count or 1)
            active = Hunk(int(first), old_left, new_left)
            change.hunks.append(active)
    if old_left or new_left:
        raise ValueError('incomplete patch')
    return changes
