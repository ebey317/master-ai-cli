from html.parser import HTMLParser
from pathlib import Path
import re

p = Path('/data/data/com.termux/files/home/taildrop/hermes-20260802_163000_430d32.html')
text = p.read_text(encoding='utf-8')

class Extractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_body = False
        self.depth = 0
        self.buf = []
        self.messages = []
        self.role = ''
    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == 'section' and 'class' in d and 'msg' in d['class']:
            self._flush()
            for c in d['class'].split():
                if c.startswith('role-'):
                    self.role = c.replace('role-','')
        if tag == 'div' and 'class' in d and 'msg-body' in d['class']:
            self.in_body = True
            self.depth = 1
        elif self.in_body:
            self.depth += 1
    def handle_endtag(self, tag):
        if self.in_body:
            self.depth -= 1
            if self.depth == 0 and tag == 'div':
                self._flush()
                self.in_body = False
    def handle_data(self, data):
        if self.in_body:
            self.buf.append(data)
    def _flush(self):
        if self.buf:
            body = re.sub(r'\s+',' ',''.join(self.buf)).strip()
            self.messages.append((self.role, body))
            self.buf = []

ext = Extractor()
ext.feed(text)

keywords = ['chain reaction','sequence','top 10','sportscenter','sports center','podcast','wrestling']
for role, body in ext.messages:
    if any(k.lower() in body.lower() for k in keywords):
        print(f"--- {role.upper()} ---")
        print(body[:1000])
        print()
