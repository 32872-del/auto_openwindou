from pathlib import Path
import re

root=Path(__file__).resolve().parents[1]
prototype=(root/'界面预览.html').read_text(encoding='utf-8')
styles=re.search(r'<style>(.*?)</style>',prototype,re.S).group(1)
(root/'ui/prototype.css').write_text(styles,encoding='utf-8')
app=root/'ui/app.js'
text=app.read_text(encoding='utf-8')
start=text.index('const statusNames=')
end=text.index('function openCreate(',start)
(root/'ui/workspace.js').write_text(text[start:end],encoding='utf-8')
app.write_text(text[:start]+text[end:],encoding='utf-8')
