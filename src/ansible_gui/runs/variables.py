"""Extra variables for a run: prompts from the roles' argument specs, and a free-form YAML block.

A role declares what it takes in meta/argument_specs.yml (or under argument_specs in meta/main.yml). Each option
becomes a prompt on the launcher. A prompt left empty sends nothing, so the inventory and the role's defaults
still apply: extra variables beat every other source, and sending a role's default would override the
inventory's value for every host.

Prompt answers reach Ansible as a vars file in the run's private directory, every string tagged !unsafe: an
answer is data, never a template. Secret answers (an option with no_log: true) are stored encrypted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from cryptography.fernet import Fernet, InvalidToken

from .. import repofiles

VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_FREEFORM = 20_000
# What typed YAML may hold once its aliases are written out. A few hundred characters of nested aliases
# (&a, *a) stand for billions of values, and showing or storing them writes every one out. Text without
# aliases can't reach this: it holds at most one value per two characters.
MAX_VALUES = MAX_FREEFORM
TOO_MANY_VALUES = (f"more than {MAX_VALUES:,} values once the YAML aliases (&name, *name) are written out. "
                   "Use fewer aliases, or put the values in a vars file in the repository.")
SECRET = "__secret__"  # a stored secret answer: {"__secret__": "<fernet token>"}
FIELD = "var."  # form field prefix: var.<name>


class _Loader(yaml.SafeLoader):
    """Reads playbooks and specs, ignoring tags like !vault and !unsafe."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else None))


def _load(path: Path):
    try:
        return yaml.load(repofiles.read_text(path), Loader=_Loader)  # noqa: S506 - SafeLoader subclass  # nosec B506
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None


def written_out(value) -> float:
    """How many values `value` holds with every YAML alias written out, counted without writing them out.

    PyYAML loads an alias as a second reference to the same list or mapping, so each one is measured once
    and its size reused wherever it appears again. inf when one contains itself. No recursion: aliases
    can nest a thousand levels in a few kilobytes."""
    sizes: dict[int, float] = {}
    open_: set[int] = set()  # the lists and mappings being measured: the path down to the one in hand
    stack = [(value, False)]
    while stack:
        v, measured = stack.pop()
        if not isinstance(v, (dict, list)):
            continue
        inside = list(v.values()) if isinstance(v, dict) else v
        if measured:
            sizes[id(v)] = 1 + sum(sizes[id(x)] if isinstance(x, (dict, list)) else 1 for x in inside)
            open_.discard(id(v))
        elif id(v) in open_:
            return float("inf")
        elif id(v) not in sizes:
            open_.add(id(v))
            stack.append((v, True))
            stack.extend((x, False) for x in inside)
    return sizes[id(value)] if isinstance(value, (dict, list)) else 1


@dataclass
class Prompt:
    name: str
    role: str
    kind: str  # text, multiline, int, float, bool, choice, multichoice, list, dict, secret
    label: str = ""
    help: str = ""
    choices: list = field(default_factory=list)
    default: object = None  # the role's default, shown as a hint; never sent
    has_default: bool = False
    required: bool = False

    @property
    def field(self) -> str:
        return FIELD + self.name

    @property
    def default_text(self) -> str:
        return show_value(self.default) if self.has_default else ""


def show_value(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


# --- finding the roles a playbook uses ----------------------------------------------------------------------------
_ROLE_TASKS = {"include_role", "import_role", "ansible.builtin.include_role", "ansible.builtin.import_role",
               "ansible.legacy.include_role", "ansible.legacy.import_role"}
_IMPORT_PLAYBOOK = {"import_playbook", "ansible.builtin.import_playbook"}
_TASK_LISTS = ("pre_tasks", "tasks", "post_tasks", "handlers")


def _static(name) -> bool:
    return isinstance(name, str) and name.strip() != "" and "{{" not in name


def _role_refs_in_tasks(tasks, out: list[tuple[str, str]]) -> None:
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            _role_refs_in_tasks(task.get(key), out)
        for key in _ROLE_TASKS & task.keys():
            args = task[key]
            if isinstance(args, str):
                args = {"name": args}
            if isinstance(args, dict) and _static(args.get("name")):
                entry = args.get("tasks_from") if _static(args.get("tasks_from")) else "main"
                out.append((args["name"].strip(), Path(str(entry)).stem))


def role_refs(playbook: Path, seen: set[Path] | None = None) -> list[tuple[str, str]]:
    """(role, entry point) for every role the playbook names statically, in play order."""
    seen = seen if seen is not None else set()
    playbook = playbook.resolve()
    if playbook in seen:
        return []
    seen.add(playbook)
    data = _load(playbook)
    out: list[tuple[str, str]] = []
    for play in data if isinstance(data, list) else []:
        if not isinstance(play, dict):
            continue
        for key in _IMPORT_PLAYBOOK & play.keys():
            if _static(play[key]):
                out += role_refs(playbook.parent / play[key].strip(), seen)
        for r in play.get("roles") or []:
            name = r if isinstance(r, str) else (r.get("role") or r.get("name")) if isinstance(r, dict) else None
            if _static(name):
                out.append((name.strip(), "main"))
        for key in _TASK_LISTS:
            _role_refs_in_tasks(play.get(key), out)
    return out


def _role_dir(name: str, playbook_dir: Path, repo_dir: Path) -> Path | None:
    if "/" in name:  # a path to the role
        candidates = [playbook_dir / name, repo_dir / name]
    elif name.count(".") >= 2:  # namespace.collection.role
        ns, coll, role = name.split(".", 2)
        candidates = [base / "collections" / "ansible_collections" / ns / coll / "roles" / role
                      for base in (playbook_dir, repo_dir)]
    else:
        candidates = [playbook_dir / "roles" / name, repo_dir / "roles" / name]
    for c in candidates:
        try:
            c = c.resolve()
        except OSError:
            continue
        if c.is_dir() and c.is_relative_to(repo_dir.resolve()):
            return c
    return None


def _spec(role_dir: Path, entry: str) -> dict | None:
    for name in ("argument_specs.yml", "argument_specs.yaml", "main.yml", "main.yaml"):
        data = _load(role_dir / "meta" / name)
        if isinstance(data, dict) and isinstance(data.get("argument_specs"), dict):
            spec = data["argument_specs"].get(entry)
            return spec if isinstance(spec, dict) else None
    return None


def _text(desc) -> str:
    if isinstance(desc, list):
        return " ".join(str(d) for d in desc)
    return str(desc) if desc else ""


def _prompt(name: str, role: str, opt: dict) -> Prompt:
    t = str(opt.get("type") or "str")
    choices = opt.get("choices") if isinstance(opt.get("choices"), list) else []
    if opt.get("no_log"):
        kind = "secret"
    elif t == "bool":
        kind = "bool"
    elif t == "int":
        kind = "choice" if choices else "int"
    elif t == "float":
        kind = "float"
    elif t == "list":
        kind = "multichoice" if choices else "list"
    elif t == "dict":
        kind = "dict"
    elif choices:
        kind = "choice"
    else:
        kind = "text"
    desc = _text(opt.get("description"))
    label, _, rest = desc.partition(". ")
    return Prompt(name=name, role=role, kind=kind, label=label.rstrip(".") if desc else name,
                  help=rest.strip(), choices=[c for c in choices if not isinstance(c, (dict, list))],
                  default=opt.get("default"), has_default="default" in opt, required=bool(opt.get("required")))


def discover(playbook: Path, repo_dir: Path) -> tuple[list[Prompt], list[str]]:
    """The prompts for a playbook, and the roles that declare none (to say why a field is missing)."""
    prompts: dict[str, Prompt] = {}
    bare: list[str] = []
    for role, entry in role_refs(playbook):
        d = _role_dir(role, playbook.parent, repo_dir)
        spec = _spec(d, entry) if d else None
        if spec is None:
            if role not in bare:
                bare.append(role)
            continue
        defaults = _role_defaults(d)
        for name, opt in (spec.get("options") or {}).items():
            if isinstance(opt, dict) and VAR_NAME.match(str(name)) and name not in prompts:
                prompts[name] = p = _prompt(str(name), role, opt)
                if name in defaults:  # what the run actually gets; the spec's default only documents it
                    p.default, p.has_default = defaults[name], True
    return list(prompts.values()), bare


def _role_defaults(role_dir: Path) -> dict:
    """The role's defaults/main.yml (or defaults/main/*.yml): the values that apply when nothing else sets them."""
    out: dict = {}
    files = [role_dir / "defaults" / n for n in ("main.yml", "main.yaml")]
    if (role_dir / "defaults" / "main").is_dir():
        files += sorted((role_dir / "defaults" / "main").glob("*.y*ml"))
    for f in files:
        data = _load(f) if f.is_file() else None
        if isinstance(data, dict):
            out.update(data)
    return out


# --- reading answers --------------------------------------------------------------------------------------------
def _convert(p: Prompt, raw) -> tuple[object, str | None]:
    """The typed value of one answer, or why it isn't one. raw is a str, or a list for multichoice."""
    if p.kind == "multichoice":
        picked = [c for c in p.choices if str(c) in {str(x) for x in raw}]
        return picked, None
    text = str(raw).strip() if p.kind not in ("multiline", "secret") else str(raw)
    if p.kind == "int":
        try:
            return int(text), None
        except ValueError:
            return None, f"{p.name} takes a whole number, like 3."
    if p.kind == "float":
        try:
            return float(text), None
        except ValueError:
            return None, f"{p.name} takes a number, like 1.5."
    if p.kind == "bool":
        return text == "true", None
    if p.kind == "choice":
        hit = next((c for c in p.choices if str(c) == text), None)
        return (hit, None) if hit is not None else (None, f"{p.name} takes one of: {', '.join(map(str, p.choices))}.")
    if p.kind == "list":
        return [line.strip() for line in text.splitlines() if line.strip()], None
    if p.kind == "dict":
        try:
            value = yaml.load(text, Loader=_Loader)  # noqa: S506  # nosec B506
        except (yaml.YAMLError, RecursionError):
            value = None
        if not isinstance(value, dict):
            return None, f"{p.name} takes YAML key: value lines."
        if written_out(value) > max(MAX_VALUES, len(text)):
            return None, f"{p.name} holds {TOO_MANY_VALUES}"
        return value, None
    return text, None


def raw_fields(src) -> dict[str, list[str]]:
    """Every var.* field in a form or query string, as lists (a multiple choice sends several)."""
    out: dict[str, list[str]] = {}
    for key in src.keys():
        if key.startswith(FIELD):
            out[key[len(FIELD):]] = [str(v) for v in src.getlist(key)] if hasattr(src, "getlist") else [str(src[key])]
    return out


def read_answers(raw: dict[str, list[str]], prompts: list[Prompt]) -> dict:
    """The answers in raw_fields(), as the launcher redraws them: {name: str, or list for a multiple choice;
    None for a secret to clear}. Empty answers are left out."""
    out = {}
    for p in prompts:
        got = raw.get(p.name) or []
        if p.kind == "multichoice":
            picked = [v for v in got if v]  # the form sends an empty marker so "none ticked" still arrives
            if picked:
                out[p.name] = picked
        elif got and got[-1] != "":
            out[p.name] = got[-1]
        if p.kind == "secret" and (raw.get(p.name + ".clear") or [""])[-1] == "1":
            out[p.name] = None
    return out


def answer_from_value(p: Prompt, value) -> str | list | dict:
    """A stored value as the launcher shows it (the reverse of _convert)."""
    if isinstance(value, dict) and SECRET in value:
        return value
    if p.kind == "multichoice":
        return [str(v) for v in value] if isinstance(value, list) else []
    if p.kind == "bool":
        return "true" if value else "false"
    if p.kind == "list":
        return "\n".join(map(str, value)) if isinstance(value, list) else str(value)
    if p.kind == "dict":
        return yaml.safe_dump(value, default_flow_style=False, sort_keys=False).strip() if isinstance(value, dict) else ""
    return show_value(value)


def answers_from_values(prompts: list[Prompt], values: dict) -> dict:
    by = {p.name: p for p in prompts}
    return {k: answer_from_value(by[k], v) for k, v in values.items() if k in by}


def values(answers: dict, prompts: list[Prompt], fernet: Fernet, kept: dict | None = None) -> tuple[dict, dict]:
    """(values to store, errors by field). An empty secret keeps the value in `kept` (the template's, or the
    schedule's being edited); a cleared one is dropped. Answers to prompts the playbook no longer has are dropped."""
    kept = kept or {}
    out, errors = {}, {}
    for p in prompts:
        if p.kind == "secret":
            raw = answers.get(p.name, "")
            if raw is None:
                continue
            if isinstance(raw, dict) and SECRET in raw:
                out[p.name] = raw
            elif raw:
                out[p.name] = {SECRET: fernet.encrypt(str(raw).encode()).decode()}
            elif isinstance(kept.get(p.name), dict) and SECRET in kept[p.name]:
                out[p.name] = kept[p.name]
            continue
        if p.name not in answers or answers[p.name] in ("", []):
            continue
        value, problem = _convert(p, answers[p.name])
        if problem:
            errors[p.field] = problem
        elif value is not None:
            out[p.name] = value
    return out, errors


def changes(now: dict, was: dict, prompts: list[Prompt]) -> list[dict]:
    """{label, was, now} for each prompt whose value differs from the template's."""
    out = []
    for p in prompts:
        a, b = now.get(p.name), was.get(p.name)
        if p.kind == "secret":
            if (a is None) != (b is None) or (a and b and a != b):
                out.append({"label": p.name, "was": "set" if b else "role default", "now": "changed" if a else "role default"})
            continue
        if a != b:
            out.append({"label": p.name, "was": "role default" if b is None else show_value(b),
                        "now": "role default" if a is None else show_value(a)})
    return out


def shown(stored: dict) -> list[tuple[str, str]]:
    """(name, value) for a run page: secrets masked."""
    return [(k, "•••••• (secret)" if isinstance(v, dict) and SECRET in v else show_value(v)) for k, v in stored.items()]


# --- free-form YAML ---------------------------------------------------------------------------------------------
def check_freeform(text: str) -> tuple[dict, str | None]:
    """The variables a free-form block sets, or what is wrong with it. The text itself is what runs."""
    if not text.strip():
        return {}, None
    if len(text) > MAX_FREEFORM:
        return {}, f"Keep extra variables under {MAX_FREEFORM:,} characters; put more in a vars file in the repository."
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506  # nosec B506
    except (yaml.YAMLError, RecursionError) as e:
        mark = getattr(e, "problem_mark", None)
        where = f" on line {mark.line + 1}" if mark else ""
        return {}, f"Extra variables aren’t valid YAML{where}. Write one name: value per line."
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, "Extra variables must be name: value lines, not a list or a single value."
    if written_out(data) > MAX_VALUES:
        return {}, f"Extra variables hold {TOO_MANY_VALUES}"
    bad = [str(k) for k in data if not VAR_NAME.match(str(k))]
    if bad:
        return {}, f"{bad[0]} isn’t a variable name: use letters, digits and underscores, not starting with a digit."
    return data, None


# --- what the run gets ------------------------------------------------------------------------------------------
class _Unsafe(str):
    pass


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Unsafe, lambda d, s: d.represent_scalar("!unsafe", str(s)))


def _mark_unsafe(value):
    if isinstance(value, str):
        return _Unsafe(value)
    if isinstance(value, list):
        return [_mark_unsafe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _mark_unsafe(v) for k, v in value.items()}
    return value


def unseal(stored: dict, fernet: Fernet) -> dict:
    """The values with secrets decrypted. Raises ValueError naming a secret that can't be decrypted."""
    out = {}
    for k, v in stored.items():
        if isinstance(v, dict) and SECRET in v:
            try:
                out[k] = fernet.decrypt(v[SECRET].encode()).decode()
            except (InvalidToken, TypeError, AttributeError):
                raise ValueError(k) from None
        else:
            out[k] = v
    return out


def vars_file_text(plain: dict) -> str:
    """YAML for `-e @file`: names plain, every string value !unsafe so Ansible never templates it."""
    return yaml.dump({k: _mark_unsafe(v) for k, v in plain.items()}, Dumper=_Dumper, sort_keys=False,
                     allow_unicode=True, default_flow_style=False)


# --- Fields or YAML -----------------------------------------------------------------------------------------------
_TAG = re.compile(r"(^|[\s:\-\[,])!\w")


def join_yaml(values: dict, rest: str) -> str:
    """The YAML view: the declared variables' values (secrets never), then the free-form text as typed."""
    head = yaml.safe_dump(values, sort_keys=False, allow_unicode=True, default_flow_style=False) if values else ""
    return (head + rest.strip("\n")).strip("\n")


def split_yaml(text: str, prompts: list[Prompt]) -> tuple[dict, str]:
    """The fields view of a YAML block: (answers for the declared variables, the rest as YAML). Text that doesn't
    parse, or uses tags like !vault, stays whole: rewriting it would lose them."""
    data, problem = check_freeform(text)
    if problem or not data or _TAG.search(text):
        return {}, text
    by = {p.name: p for p in prompts if p.kind != "secret"}
    answers = {k: answer_from_value(by[k], v) for k, v in data.items() if k in by}
    if not answers:
        return {}, text
    rest = {k: v for k, v in data.items() if k not in answers}
    return answers, yaml.safe_dump(rest, sort_keys=False, allow_unicode=True).strip("\n") if rest else ""


# --- describing a playbook ----------------------------------------------------------------------------------------
@dataclass
class About:
    comment: str = ""  # the comment block at the top of the file
    roles: list[tuple[str, str]] = field(default_factory=list)  # (role, its description) in play order
    plays: list[str] = field(default_factory=list)  # play names


def _top_comment(path: Path) -> str:
    """The comment block at the top of the file, as it was written: its line breaks and the indentation inside
    it stay (a list, a command to copy), and only the "#" and the one space after it go. A run of empty comment
    lines is one blank line; there is none at the start or the end."""
    lines: list[str] = []
    try:
        text = repofiles.read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        s = line.strip()
        if not lines and (s in ("", "---") or s.startswith("#!")):
            continue
        if not s.startswith("#"):
            break
        body = s.lstrip("#")
        body = (body[1:] if body.startswith(" ") else body).rstrip()
        if re.match(r"^(-\*-|vim?:|yaml-language-server|code:|noqa|yamllint)", body.strip()):
            continue
        if not body.strip():
            if lines and lines[-1]:
                lines.append("")
            continue
        lines.append(body)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _role_description(role_dir: Path) -> str:
    for name in ("main.yml", "main.yaml"):
        data = _load(role_dir / "meta" / name)
        if isinstance(data, dict):
            info = data.get("galaxy_info")
            if isinstance(info, dict) and info.get("description"):
                return " ".join(str(info["description"]).split())
    spec = _spec(role_dir, "main")
    return " ".join(_text(spec.get("short_description")).split()) if spec else ""


def describe(playbook: Path, repo_dir: Path) -> About:
    about = About(comment=_top_comment(playbook))
    seen = set()
    for role, _ in role_refs(playbook):
        if role in seen:
            continue
        seen.add(role)
        d = _role_dir(role, playbook.parent, repo_dir)
        about.roles.append((role, _role_description(d) if d else ""))

    def plays(path: Path, visited: set) -> None:
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        data = _load(path)
        for play in data if isinstance(data, list) else []:
            if not isinstance(play, dict):
                continue
            for key in _IMPORT_PLAYBOOK & play.keys():
                if _static(play[key]):
                    plays(path.parent / play[key].strip(), visited)
            if isinstance(play.get("name"), str) and play["name"].strip():
                about.plays.append(" ".join(play["name"].split()))

    plays(playbook, set())
    return about


def summary(about: About) -> str:
    """What a playbook does: its top comment as written, else its roles' descriptions, else its play names."""
    if about.comment:
        return about.comment
    described = [f"{role}: {desc}" for role, desc in about.roles if desc]
    if described:
        return "; ".join(described)
    return "; ".join(about.plays)
