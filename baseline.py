import os
import re
import sys
from google import genai
from google.genai import types

MODEL = "gemini-3.5-flash-lite"
TEMPERATURE = 0.3

if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
    saida = sys.argv[1]
else:
    contador = 1
    while True:
        saida = f"baseline{contador:03d}.bpmn"
        if not os.path.exists(saida):
            break
        contador += 1

print(f"Arquivo selecionado para uso: {saida}")

prompt = f"""
Crie o código BPMN 2.0 em XML COMPLETO para o processo descrito abaixo.

REQUISITOS:
1. Organize o processo em swimlanes/pools por ator responsável (normalmente de 3 a 6 atores).
2. Use eventos de início e fim, tarefas, gateways exclusivos com todos os ramos,
   fluxos de mensagem entre atores, ciclos de rejeição/revisão e eventos de temporização
   quando houver prazos.
3. Seja detalhado: no mínimo 15 nós.
4. Inclua a seção de Diagram Interchange completa (BPMNDiagram, BPMNPlane, BPMNShape com
   dc:Bounds e BPMNEdge com di:waypoint) para que o arquivo seja aberto e renderizado
   no Camunda Modeler e no bpmn.io.

Processo: {text}

Retorne APENAS o XML, sem explicações e sem blocos de markdown.
"""

resp = client.models.generate_content(
    model=MODEL,
    contents=prompt,
    config=types.GenerateContentConfig(temperature=TEMPERATURE),
)

xml = (resp.text or "").strip()
# remove cercas de markdown, se o modelo as incluir
xml = re.sub(r'^```(?:xml)?\s*', '', xml)
xml = re.sub(r'\s*```$', '', xml)

with open(saida, 'w', encoding='utf-8') as f:
    f.write(xml)

print(f"Baseline salvo em {saida}")
