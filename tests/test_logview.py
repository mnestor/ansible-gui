"""The run log's colour: which lines say what, and that pieces of a log render as the whole does."""

from ansible_gui.runs import logview

PLAYBOOK = """$ ansible-playbook -i /repos/1/commits/abc/inventory/hosts.yml --diff /repos/2/commits/def/site.yml

[ansible-gui] Pulling lab (main) from git@example.test:me/lab.git

PLAY [Configure the lab] *******************************************************

TASK [Gathering Facts] *********************************************************
ok: [box1]
fatal: [box3]: UNREACHABLE! => {"changed": false, "msg": "Failed to connect", "unreachable": true}

TASK [Write the motd] **********************************************************
--- before: /etc/motd
+++ after: /etc/motd
@@ -1,2 +1,2 @@
 lab
-old line
+new line

changed: [box1]
skipping: [box2]
[WARNING]: Platform linux on host box2 is using the discovered Python
interpreter at /usr/bin/python3.

TASK [Break] *******************************************************************
fatal: [box2]: FAILED! => {
    "msg": "<script>alert(1)</script>"
}
...ignoring

PLAY RECAP *********************************************************************
box1                       : ok=2    changed=1    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
box2                       : ok=1    changed=0    unreachable=0    failed=1    skipped=1    rescued=0    ignored=1
box3                       : ok=0    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0
box4                       : ok=3    changed=0    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
"""


def kinds(text: str, state: str = "") -> list[tuple[str, str]]:
    """(kind, line) for each line, read back from the HTML render() makes."""
    import html
    import re
    out, (markup, _) = [], logview.render(text, state)
    for kind, body, plain in re.findall(r'<span class="ll ll-(\w+)">(.*?)</span>|([^<]+)', str(markup), re.S):
        out += [(kind, html.unescape(line)) for line in (body or plain).splitlines()]
    return out


def test_each_line_takes_the_status_it_reports():
    got = dict((line, kind) for kind, line in kinds(PLAYBOOK))
    assert got["ok: [box1]"] == "ok" and got["changed: [box1]"] == "changed" and got["skipping: [box2]"] == "skipped"
    assert got['fatal: [box3]: UNREACHABLE! => {"changed": false, "msg": "Failed to connect", "unreachable": true}'] == "unreachable"
    assert got["PLAY [Configure the lab] " + "*" * 55] == "head" and got["PLAY RECAP " + "*" * 69] == "head"
    assert got["[ansible-gui] Pulling lab (main) from git@example.test:me/lab.git"] == "note"
    # A result's other lines, and a warning's second line, belong to it; a blank line ends that.
    assert got["fatal: [box2]: FAILED! => {"] == got['    "msg": "<script>alert(1)</script>"'] == got["}"] == "failed"
    assert got["interpreter at /usr/bin/python3."] == "warn" and got["...ignoring"] == "skipped"
    # --diff: added and removed lines, not the context line; and a line starting with - elsewhere is not a removal
    assert got["+new line"] == "add" and got["-old line"] == "del" and got[" lab"] == "" and got["@@ -1,2 +1,2 @@"] == "note"
    # The recap takes each host's worst result.
    recap = {line.split()[0]: kind for kind, line in kinds(PLAYBOOK) if " : ok=" in line}
    assert recap == {"box1": "changed", "box2": "failed", "box3": "unreachable", "box4": "ok"}


def test_a_command_output_takes_its_hosts_status_and_text_is_never_markup():
    adhoc = "box1 | CHANGED | rc=0 >>\n-rw-r--r-- 1 root root 0 motd\n<b>bold</b>\n\nbox2 | UNREACHABLE! => {\n}\nbox3 | SUCCESS | 42 facts gathered and saved\n"
    got = dict((line, kind) for kind, line in kinds(adhoc))
    assert got["box1 | CHANGED | rc=0 >>"] == got["-rw-r--r-- 1 root root 0 motd"] == got["<b>bold</b>"] == "changed"
    assert got["box2 | UNREACHABLE! => {"] == "unreachable" and got["box3 | SUCCESS | 42 facts gathered and saved"] == "ok"
    markup, _ = logview.render(adhoc)
    assert "<b>" not in markup and "&lt;b&gt;bold&lt;/b&gt;" in markup
    assert "<script>" not in logview.render(PLAYBOOK)[0]


def test_a_log_read_in_pieces_is_coloured_like_the_whole():
    lines = PLAYBOOK.splitlines(keepends=True)
    whole = kinds(PLAYBOOK)
    for cut in range(len(lines) + 1):  # the page's first piece may end anywhere, mid-result and mid-diff included
        first, state = logview.render("".join(lines[:cut]))
        assert kinds("".join(lines[:cut])) + kinds("".join(lines[cut:]), state) == whole, (cut, state)
    # The state comes back from the browser: anything else in it is ignored, not put into the page.
    assert logview.render("more\n", 'x"><script>')[0] == "more\n"
    assert logview.render("more\n", "failed")[0] == '<span class="ll ll-failed">more\n</span>'
