import os, json, re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from google import genai
from google.genai import types
from sys import argv

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])  # não deixe a chave no código
MODEL = "gemini-3.5-flash-lite"   


raias = 'automático'
process = 'n'  # 'y' = lanes no mesmo pool | 'n' = pools independentes

if len(argv) > 1 and not argv[1].startswith('-'):
    name = argv[1]
else:
    contador = 1
    while True:
        name = f"{contador:03d}.bpmn"
        if not os.path.exists(name):
            break
        contador += 1

print(f"Arquivo selecionado para uso: {name}")




# ===== DETECÇÃO E CORREÇÃO DE CICLOS =====
def detect_and_fix_cycles(data, text):
    """Detecta menções a 'volta/retorna' e adiciona back-edges ausentes"""
    print("\n🔄 Analisando ciclos no texto...")
    cycle_keywords = ['rejeita', 'volta', 'retorna', 'recomeça', 'reenvia', 'revisar', 'reenviar']
    text_lower = text.lower()
    has_cycle_mention = any(keyword in text_lower for keyword in cycle_keywords)

    if not has_cycle_mention:
        print("   ✓ Nenhum ciclo detectado no texto")
        return data

    rejection_nodes = []
    approval_nodes = []

    for node in data['nodes']:
        label_lower = node.get('label', '').lower()
        if any(kw in label_lower for kw in ['rejeita', 'recusa', 'nega', 'volta', 'revisar']):
            rejection_nodes.append(node)
        if any(kw in label_lower for kw in ['aprovar', 'avaliar', 'analisar', 'verificar']):
            approval_nodes.append(node)

    layers_result = assign_layers_simple(data['nodes'], data['edges'])
    layers = layers_result[0]

    layer_map = {}
    for idx, layer in enumerate(layers):
        for node_id in layer:
            layer_map[node_id] = idx

    added_cycles = 0
    for rej_node in rejection_nodes:
        rej_layer = layer_map.get(rej_node['id'], 0)
        for appr_node in approval_nodes:
            appr_layer = layer_map.get(appr_node['id'], 0)
            if (rej_node.get('lane') == appr_node.get('lane') and
                appr_layer < rej_layer and
                rej_layer - appr_layer <= 3):
                edge_exists = any(
                    e['source'] == rej_node['id'] and e['target'] == appr_node['id']
                    for e in data['edges']
                )
                if not edge_exists:
                    data['edges'].append({
                        'source': rej_node['id'],
                        'target': appr_node['id'],
                        'label': 'Rejeitar/Revisar'
                    })
                    added_cycles += 1
                    print(f"   + Ciclo adicionado: {rej_node['label']} → {appr_node['label']}")

    if added_cycles > 0:
        print(f"✅ {added_cycles} ciclos de volta adicionados!")
    else:
        print("   ℹ️ Nenhum ciclo novo necessário")
    return data

# ===== INJEÇÃO DE EVENTOS TEMPORAIS =====
def inject_timer_events(data, text):
    """Adiciona timer events quando texto menciona prazos"""
    print("\n⏰ Analisando eventos temporais...")
    time_pattern = r'(\d+)\s*(hora|dia|minuto|segundo|h|d|min)s?'
    matches = list(re.finditer(time_pattern, text.lower()))

    if not matches:
        print("   ✓ Nenhum prazo temporal detectado")
        return data

    timers_added = 0
    for match in matches:
        duration = match.group(1)
        unit = match.group(2)
        context_start = max(0, match.start() - 100)
        context_end = min(len(text), match.end() + 100)
        context_text = text[context_start:context_end].lower()

        related_node = None
        for node in data['nodes']:
            node_label = node.get('label', '').lower()
            if node_label and node_label in context_text:
                if any(kw in node_label for kw in ['receb', 'aguard', 'espera', 'verific']):
                    related_node = node
                    break

        if related_node:
            timer_id = f"timer_{related_node['id']}"
            if not any(n['id'] == timer_id for n in data['nodes']):
                unit_map = {
                    'h': 'horas', 'hora': 'horas',
                    'd': 'dias', 'dia': 'dias',
                    'min': 'minutos', 'minuto': 'minutos',
                    'segundo': 'segundos'
                }
                unit_normalized = unit_map.get(unit, unit)
                timer_node = {
                    'id': timer_id,
                    'label': f'Prazo {duration} {unit_normalized}',
                    'type': 'intermediateTimerEvent',
                    'lane': related_node['lane']
                }
                data['nodes'].append(timer_node)

                for edge in data['edges']:
                    if edge['source'] == related_node['id']:
                        original_target = edge['target']
                        edge['target'] = timer_id
                        data['edges'].append({
                            'source': timer_id,
                            'target': original_target
                        })
                        timers_added += 1
                        print(f"   + Timer adicionado: {related_node['label']} → [{duration}{unit}] → destino")
                        break

    if timers_added > 0:
        print(f"✅ {timers_added} eventos temporais adicionados!")
    else:
        print("   ℹ️ Nenhum timer novo necessário")
    return data

# ===== VALIDAÇÃO ESTRUTURAL (POOLS INDEPENDENTES) =====
def fix_bpmn_structure(data, independent_pools=False):
    """Corrige estruturas inválidas APENAS em modo pools independentes"""

    if not independent_pools:
        print("\n✓ Modo lanes no mesmo pool - validação estrutural não necessária")
        return data

    print("\n🔧 Validando estrutura BPMN (pools independentes)...")

    nodes_dict = {n['id']: n for n in data['nodes']}
    new_nodes = []
    new_edges = []
    fixes_applied = 0

    for edge in data['edges'][:]:  # Cópia para iterar seguro
        src = nodes_dict.get(edge['source'])
        tgt = nodes_dict.get(edge['target'])

        if not src or not tgt:
            continue

        src_lane = src.get('lane')
        tgt_lane = tgt.get('lane')

        # Detecta mudança de pool (lane diferente)
        if src_lane != tgt_lane:
            src_type = src.get('type', '')

            needs_intermediate = False
            task_label = ""

            # Regra 1 e 2: MessageEvents precisam de tarefa antes de mudar pool
            if 'MessageEvent' in src_type or 'messageEvent' in src_type.lower():
                needs_intermediate = True
                task_label = f"Processar {src.get('label', 'mensagem')}"

            # Regra 3 e 4: Gateways precisam de tarefa antes de mudar pool
            elif 'Gateway' in src_type or 'gateway' in src_type.lower():
                needs_intermediate = True
                task_label = f"Executar ação"

            if needs_intermediate:
                # Cria nó intermediário
                inter_id = f"auto_task_{src['id']}_{tgt['id']}"

                if inter_id not in nodes_dict:
                    inter_node = {
                        'id': inter_id,
                        'label': task_label,
                        'type': 'task',
                        'lane': src_lane  # Mesma lane da origem
                    }
                    new_nodes.append(inter_node)
                    nodes_dict[inter_id] = inter_node

                    # Ajusta edges: src → intermediate → tgt
                    edge['target'] = inter_id
                    new_edges.append({
                        'source': inter_id,
                        'target': tgt['id'],
                        'label': edge.get('label', '')
                    })

                    fixes_applied += 1
                    print(f"   + Tarefa intermediária: {src['label']} → [{task_label}] → {tgt['label']}")

    # Adiciona novos nós e edges
    data['nodes'].extend(new_nodes)
    data['edges'].extend(new_edges)

    if fixes_applied > 0:
        print(f"✅ {fixes_applied} correções estruturais aplicadas!")
    else:
        print("   ✓ Estrutura válida!")

    return data

def build_graph(nodes, edges):
    """Constrói estruturas de grafo"""
    graph = defaultdict(list)
    reverse_graph = defaultdict(list)
    for edge in edges:
        src, tgt = edge['source'], edge['target']
        graph[src].append(tgt)
        reverse_graph[tgt].append(src)
    return graph, reverse_graph

def assign_layers_simple(nodes, edges):
    """Atribui camadas de forma simples e garantida"""
    graph, reverse_graph = build_graph(nodes, edges)
    start_nodes = [n['id'] for n in nodes if n.get('type', '').startswith('start')]
    if not start_nodes:
        start_nodes = [n['id'] for n in nodes if not reverse_graph[n['id']]]
    if not start_nodes:
        start_nodes = [nodes[0]['id']]

    layers = defaultdict(list)
    visited = set()
    queue = deque([(nid, 0) for nid in start_nodes])
    max_depth = 25

    while queue:
        node_id, depth = queue.popleft()
        if node_id in visited or depth > max_depth:
            continue
        visited.add(node_id)
        layers[depth].append(node_id)
        for child in graph[node_id]:
            if child not in visited:
                queue.append((child, depth + 1))

    for node in nodes:
        if node['id'] not in visited:
            layers[max_depth].append(node['id'])

    max_layer = max(layers.keys()) if layers else 0
    result = [layers[i] for i in range(max_layer + 1)]
    return result, graph, reverse_graph

LANE_MIN_H   = 140   # altura mínima da raia
NODE_STEP    = 90    # espaço vertical por nó empilhado
LANE_PAD     = 30    # margem interna em cima/embaixo
POOL_SPACING = 20    # espaço entre pools

def compute_lane_heights(layers, nodes_dict, lanes):
    max_stack = defaultdict(int)
    for layer in layers:
        per_lane = defaultdict(int)
        for nid in layer:
            per_lane[nodes_dict[nid].get('lane', lanes[0]['id'])] += 1
        for lid, c in per_lane.items():
            max_stack[lid] = max(max_stack[lid], c)
    return {
        lane['id']: max(LANE_MIN_H, max_stack.get(lane['id'], 1) * NODE_STEP + 2 * LANE_PAD)
        for lane in lanes
    }

def calculate_positions_with_lanes(layers, nodes_dict, lanes, independent_pools=False):
    positions = {}
    x_spacing = 180
    start_x = 180
    start_y = 80
    pool_spacing = POOL_SPACING if independent_pools else 0

    lane_heights = compute_lane_heights(layers, nodes_dict, lanes)

    lane_y_base = {}
    cumulative_y = start_y
    for idx, lane in enumerate(lanes):
        lane_y_base[lane['id']] = cumulative_y
        lane_h = lane_heights[lane['id']]
        cumulative_y += lane_h + (pool_spacing if independent_pools else 0)

    layer_lane_nodes = defaultdict(lambda: defaultdict(list))
    for layer_idx, layer in enumerate(layers):
        for node_id in layer:
            lane_id = nodes_dict[node_id].get('lane', lanes[0]['id'] if lanes else 'default')
            layer_lane_nodes[layer_idx][lane_id].append(node_id)

    for layer_idx in range(len(layers)):
        x = start_x + layer_idx * x_spacing
        for lane_id, nodes_in_lane in layer_lane_nodes[layer_idx].items():
            y_base = lane_y_base.get(lane_id, start_y)
            lane_h = lane_heights[lane_id]
            stack_h = len(nodes_in_lane) * NODE_STEP
            for node_idx, node_id in enumerate(nodes_in_lane):
                node = nodes_dict[node_id]
                node_type = node.get('type', 'task')

                if 'Event' in node_type or node_type in ['start', 'end']:
                    width, height = 36, 36
                elif node_type in ['gateway', 'exclusiveGateway', 'parallelGateway', 'inclusiveGateway']:
                    width, height = 50, 50
                else:
                    width, height = 120, 80

                y = y_base + (lane_h - stack_h) / 2 + node_idx * NODE_STEP + (NODE_STEP - height) / 2
                positions[node_id] = {
                    'x': x, 'y': y, 'width': width, 'height': height,
                    'center_x': x + width / 2, 'center_y': y + height / 2,
                    'layer': layer_idx, 'lane': lane_id
                }

    return positions, lane_y_base, lane_heights

def create_waypoints_with_ports(src_pos, tgt_pos, edge_offset=0):
    """Cria waypoints conectando nas bordas dos nós"""
    exit_x = src_pos['x'] + src_pos['width']
    exit_y = src_pos['center_y'] + edge_offset
    entry_x = tgt_pos['x']
    entry_y = tgt_pos['center_y'] + edge_offset

    if src_pos['layer'] >= tgt_pos['layer']:
        if src_pos['lane'] == tgt_pos['lane']:
            arc_y = min(exit_y, entry_y) - 40 - abs(edge_offset) * 0.3 if exit_y < entry_y else max(exit_y, entry_y) + 40 + abs(edge_offset) * 0.3
        else:
            arc_y = (exit_y + entry_y) / 2
        margin = 50 + abs(edge_offset) * 0.2
        waypoints = [
            (exit_x, exit_y), (exit_x + margin, exit_y),
            (exit_x + margin, arc_y), (entry_x - margin, arc_y),
            (entry_x - margin, entry_y), (entry_x, entry_y)
        ]
    else:
        if abs(exit_y - entry_y) < 15:
            waypoints = [(exit_x, exit_y), (entry_x, entry_y)]
        else:
            mid_x = (exit_x + entry_x) / 2 + edge_offset * 0.6
            h_margin = 30
            waypoints = [
                (exit_x, exit_y), (exit_x + h_margin, exit_y),
                (mid_x, exit_y), (mid_x, entry_y),
                (entry_x - h_margin, entry_y), (entry_x, entry_y)
            ]
    return waypoints

def create_bpmn_element(process, node):
    """Cria elemento BPMN correto baseado no tipo"""
    nid = node['id']
    label = node.get('label', node.get('name', ''))
    ntype = node.get('type', 'task')

    if ntype in ['start', 'startEvent']:
        elem = ET.SubElement(process, 'bpmn:startEvent', {'id': nid, 'name': label})
    elif ntype == 'startMessageEvent':
        elem = ET.SubElement(process, 'bpmn:startEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:messageEventDefinition')
    elif ntype == 'startTimerEvent':
        elem = ET.SubElement(process, 'bpmn:startEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:timerEventDefinition')
    elif ntype == 'intermediateCatchEvent':
        elem = ET.SubElement(process, 'bpmn:intermediateCatchEvent', {'id': nid, 'name': label})
    elif ntype == 'intermediateMessageEvent':
        elem = ET.SubElement(process, 'bpmn:intermediateCatchEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:messageEventDefinition')
    elif ntype == 'intermediateTimerEvent':
        elem = ET.SubElement(process, 'bpmn:intermediateCatchEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:timerEventDefinition')
    elif ntype == 'intermediateThrowEvent':
        elem = ET.SubElement(process, 'bpmn:intermediateThrowEvent', {'id': nid, 'name': label})
    elif ntype == 'intermediateThrowMessageEvent':
        elem = ET.SubElement(process, 'bpmn:intermediateThrowEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:messageEventDefinition')
    elif ntype in ['end', 'endEvent']:
        elem = ET.SubElement(process, 'bpmn:endEvent', {'id': nid, 'name': label})
    elif ntype == 'endMessageEvent':
        elem = ET.SubElement(process, 'bpmn:endEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:messageEventDefinition')
    elif ntype == 'task':
        elem = ET.SubElement(process, 'bpmn:task', {'id': nid, 'name': label})
    elif ntype == 'userTask':
        elem = ET.SubElement(process, 'bpmn:userTask', {'id': nid, 'name': label})
    elif ntype == 'serviceTask':
        elem = ET.SubElement(process, 'bpmn:serviceTask', {'id': nid, 'name': label})
    elif ntype == 'sendTask':
        elem = ET.SubElement(process, 'bpmn:sendTask', {'id': nid, 'name': label})
    elif ntype == 'receiveTask':
        elem = ET.SubElement(process, 'bpmn:receiveTask', {'id': nid, 'name': label})
    elif ntype in ['gateway', 'exclusiveGateway']:
        elem = ET.SubElement(process, 'bpmn:exclusiveGateway', {'id': nid, 'name': label})
    elif ntype == 'parallelGateway':
        elem = ET.SubElement(process, 'bpmn:parallelGateway', {'id': nid, 'name': label})
    elif ntype == 'inclusiveGateway':
        elem = ET.SubElement(process, 'bpmn:inclusiveGateway', {'id': nid, 'name': label})
    elif ntype == 'endErrorEvent':
        elem = ET.SubElement(process, 'bpmn:endEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:errorEventDefinition')
    elif ntype == 'endTerminateEvent':
        elem = ET.SubElement(process, 'bpmn:endEvent', {'id': nid, 'name': label})
        ET.SubElement(elem, 'bpmn:terminateEventDefinition')
    else:
        elem = ET.SubElement(process, 'bpmn:task', {'id': nid, 'name': label})

    return elem

def should_be_message_flow(src_node, tgt_node, positions):
    """Determina se a aresta deve ser message flow"""
    if src_node['id'] not in positions or tgt_node['id'] not in positions:
        return False
    src_lane = positions[src_node['id']]['lane']
    tgt_lane = positions[tgt_node['id']]['lane']
    return src_lane != tgt_lane

# ==================== PROCESSAMENTO ====================

print("🔄 Chamando Gemini...")


# Monta prompt condicional baseado no modo
if process == 'n':
    structural_rules = """
⚠️ REGRAS ESTRUTURAIS OBRIGATÓRIAS (POOLS INDEPENDENTES):

Como você está criando POOLS INDEPENDENTES, a comunicação entre lanes DEVE seguir estas regras:

1. **Message Start Event**:
   - DEVE ter pelo menos 1 tarefa na MESMA lane antes de mudar de pool
   - NÃO pode receber aresta de nó anterior
   - Exemplo: [StartMessageEvent] → [Tarefa X] → [SendTask para outro pool]

2. **IntermediateMessageEvent (catch)**:
   - DEVE ter pelo menos 1 tarefa na MESMA lane antes de mudar de pool
   - Exemplo: [IntermediateMessageEvent] → [Tarefa Y] → [SendTask]

3. **ExclusiveGateway (decisão)**:
   - Para cada decisão DEVE ter pelo menos 1 tarefa na MESMA lane APÓS o gateway antes de mudar de pool
   - Exemplo: [Gateway] → [Tarefa A] → [SendTask para outro pool]

4. **ParallelGateway**:
   - DEVE ter pelo menos 1 tarefa na MESMA lane em CADA branch antes de mudar de pool

5. **Message Flows entre pools**:
   - Use: [SendTask/ThrowMessageEvent na Lane A] → [ReceiveTask/MessageEvent na Lane B] → [Tarefa na Lane B]
   - NUNCA conecte diretamente gateways ou eventos entre pools diferentes sem tarefa intermediária

6. **EndMessageEvent e EndEvent**:
   - NUNCA podem receber arestas diretamente de outro pool
   - NUNCA DEVE estar sem aresta anterior
   - DEVE ter pelo menos 1 tarefa na MESMA lane ANTES do evento de fim
   - Exemplo CORRETO: [Tarefa na Lane A] → [EndMessageEvent na Lane A]
   - Exemplo ERRADO: [Nó na Lane B] → [EndMessageEvent na Lane A] ❌
   - Se precisar finalizar após receber de outro pool: [MessageEvent] → [Tarefa Final] → [EndEvent]

"""
else:
    structural_rules = """
ℹ️ MODO LANES NO MESMO POOL:
- Você pode conectar nós diretamente entre lanes diferentes usando sequence flows normais
- Não há restrições especiais para mudanças entre lanes
- Message flows são opcionais, use apenas quando fizer sentido semântico
"""

prompt = f"""
use {raias}.

Crie JSON BPMN com SWIMLANES e EVENTOS DE MENSAGEM.

TIPOS DE NÓS DISPONÍVEIS:
- **Eventos de Início**: start, startMessageEvent, startTimerEvent
- **Eventos Intermediários**: intermediateCatchEvent, intermediateMessageEvent, intermediateThrowEvent, intermediateThrowMessageEvent, intermediateTimerEvent
- **Eventos de Fim**: end, endMessageEvent
- **Tarefas**: task, userTask, serviceTask, sendTask, receiveTask
- **Gateways**: gateway, exclusiveGateway, parallelGateway, inclusiveGateway

{structural_rules}

INSTRUÇÕES GERAIS:
1. Use "startMessageEvent" quando o processo começa com recebimento de mensagem
2. Use "intermediateMessageEvent" para receber mensagens durante o processo
3. Use "intermediateThrowMessageEvent" ou "sendTask" para enviar mensagens
4. Use "intermediateTimerEvent" para eventos com prazo/tempo
5. Use "endMessageEvent" quando o processo termina com envio de mensagem
6. Use "receiveTask" para tarefas que aguardam mensagens
7. Identifique TODOS os atores relevantes (normalmente de 3 a 6 swimlanes). Não limite a 3.
8. Seja detalhado: modele no mínimo 15 nós, com todos os ramos de cada gateway,
   ciclos de rejeição/revisão, tratamento de erros e timeouts.
9. O JSON de exemplo abaixo mostra APENAS o formato. O seu resultado deve ser
   muito mais completo que o exemplo.

Formato JSON:
{{
  "lanes": [
    {{"id": "lane1", "name": "Colaborador"}},
    {{"id": "lane2", "name": "Gerência"}}
  ],
  "nodes": [
    {{"id": "start1", "label": "Início", "type": "start", "lane": "lane1"}},
    {{"id": "n1", "label": "Enviar Solicitação", "type": "sendTask", "lane": "lane1"}},
    {{"id": "n2", "label": "Receber Mensagem", "type": "intermediateMessageEvent", "lane": "lane2"}},
    {{"id": "g1", "label": "Aprovado?", "type": "exclusiveGateway", "lane": "lane2"}},
    {{"id": "end1", "label": "Fim", "type": "endMessageEvent", "lane": "lane1"}}
  ],
  "edges": [
    {{"source": "start1", "target": "n1"}},
    {{"source": "n1", "target": "n2"}},
    {{"source": "n2", "target": "g1"}},
    {{"source": "g1", "target": "end1", "label": "Sim"}}
  ]
}}

Processo: {text}

Retorne APENAS o JSON:
"""

resp = client.models.generate_content(
    model=MODEL,
    contents=prompt,
    config=types.GenerateContentConfig(
        response_mime_type="application/json",  # força JSON válido
        temperature=0.3,
    ),
)

try:
    data = json.loads(resp.text)
except Exception:
    match = re.search(r'\{.*\}', resp.text or "", re.DOTALL)
    data = json.loads(match.group()) if match else {"lanes": [], "nodes": [], "edges": []}

print(f"✓ {len(data.get('lanes', []))} raias, {len(data.get('nodes', []))} nós")

# ===== SALVA O JSON INTERMEDIÁRIO =====
# Troca a extensão .bpmn por .json para o arquivo de texto
json_filename = name.replace('.bpmn', '.json')
with open(json_filename, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"💾 JSON intermediário salvo em: {json_filename}")
# ======================================


# Aplica workarounds
data = detect_and_fix_cycles(data, text)
data = inject_timer_events(data, text)
data = fix_bpmn_structure(data, independent_pools=(process == 'n'))  # Validação condicional

lanes = data.get('lanes', [])
if not lanes:
    lanes = [{"id": "lane1", "name": "Processo"}]

for node in data['nodes']:
    if 'lane' not in node:
        node['lane'] = lanes[0]['id']

nodes_dict = {n['id']: n for n in data['nodes']}

print("\n🎨 Calculando layout...")
layers, graph, reverse_graph = assign_layers_simple(data['nodes'], data['edges'])
positions, lane_y_base, lane_heights = calculate_positions_with_lanes(layers, nodes_dict, lanes, independent_pools=(process == 'n'))

# ==================== GERA BPMN ====================

bpmn = ET.Element('bpmn:definitions', {
    'xmlns:bpmn': "http://www.omg.org/spec/BPMN/20100524/MODEL",
    'xmlns:bpmndi': "http://www.omg.org/spec/BPMN/20100524/DI",
    'xmlns:dc': "http://www.omg.org/spec/DD/20100524/DC",
    'xmlns:di': "http://www.omg.org/spec/DD/20100524/DI",
    'id': 'Definitions_1',
    'targetNamespace': 'http://bpmn.io/schema/bpmn'
})

collaboration = ET.SubElement(bpmn, 'bpmn:collaboration', {'id': 'Collaboration_1'})

if process == 'y':
    ET.SubElement(collaboration, 'bpmn:participant', {
        'id': 'Participant_Process',
        'processRef': 'Process_1'
    })
else:
    for lane in lanes:
        ET.SubElement(collaboration, 'bpmn:participant', {
            'id': f'Participant_{lane["id"]}',
            'name': lane['name'],
            'processRef': f'Process_{lane["id"]}'
        })

# Criação dos processes
if process == 'y':
    process_elem = ET.SubElement(bpmn, 'bpmn:process', {'id': 'Process_1', 'isExecutable': 'false'})
    lane_set = ET.SubElement(process_elem, 'bpmn:laneSet', {'id': 'LaneSet_1'})

    for lane in lanes:
        lane_elem = ET.SubElement(lane_set, 'bpmn:lane', {'id': lane['id'], 'name': lane['name']})
        for node in data['nodes']:
            if node.get('lane') == lane['id']:
                ET.SubElement(lane_elem, 'bpmn:flowNodeRef').text = node['id']

    for node in data['nodes']:
        create_bpmn_element(process_elem, node)

else:
    process_elements = {}
    for lane in lanes:
        proc = ET.SubElement(bpmn, 'bpmn:process', {
            'id': f'Process_{lane["id"]}',
            'isExecutable': 'false'
        })
        process_elements[lane['id']] = proc

    for node in data['nodes']:
        node_lane = node.get('lane', lanes[0]['id'])
        create_bpmn_element(process_elements[node_lane], node)

# Separar sequence flows e message flows
sequence_flows = []
message_flows = []

for edge in data['edges']:
    src_node = nodes_dict.get(edge['source'])
    tgt_node = nodes_dict.get(edge['target'])

    if process == 'n':
        if src_node and tgt_node and should_be_message_flow(src_node, tgt_node, positions):
            message_flows.append(edge)
        else:
            sequence_flows.append(edge)
    else:
        if src_node and tgt_node and should_be_message_flow(src_node, tgt_node, positions):
            message_flows.append(edge)
        else:
            sequence_flows.append(edge)

# Criar sequence flows
for edge in sequence_flows:
    fid = f"Flow_{edge['source']}_{edge['target']}"

    if process == 'y':
        parent_elem = process_elem
    else:
        src_lane = nodes_dict[edge['source']].get('lane', lanes[0]['id'])
        parent_elem = process_elements[src_lane]

    seq = ET.SubElement(parent_elem, 'bpmn:sequenceFlow', {
        'id': fid, 'sourceRef': edge['source'], 'targetRef': edge['target']
    })
    if edge.get('label'):
        seq.set('name', edge['label'])

# Criar message flows
for edge in message_flows:
    fid = f"MessageFlow_{edge['source']}_{edge['target']}"
    msg = ET.SubElement(collaboration, 'bpmn:messageFlow', {
        'id': fid, 'sourceRef': edge['source'], 'targetRef': edge['target']
    })
    if edge.get('label'):
        msg.set('name', edge['label'])

# ==================== DIAGRAMA ====================

diagram = ET.SubElement(bpmn, 'bpmndi:BPMNDiagram', {'id': 'BPMNDiagram_1'})
plane = ET.SubElement(diagram, 'bpmndi:BPMNPlane', {'id': 'BPMNPlane_1', 'bpmnElement': 'Collaboration_1'})

pool_spacing = POOL_SPACING if process == 'n' else 0
lane_width = max(800, 180 + len(layers) * 180 + 100)  # Largura mínima 800

if process == 'y':
    total_height = sum(lane_heights[lane['id']] for lane in lanes)

    shape = ET.SubElement(plane, 'bpmndi:BPMNShape', {
        'id': 'Participant_Process_di',
        'bpmnElement': 'Participant_Process',
        'isHorizontal': 'true'
    })
    ET.SubElement(shape, 'dc:Bounds', {
        'x': '100', 'y': '80', 'width': str(lane_width), 'height': str(total_height)
    })

    cumulative_y = 80
    for lane in lanes:
        lane_h = lane_heights[lane['id']]
        shape = ET.SubElement(plane, 'bpmndi:BPMNShape', {
            'id': f"{lane['id']}_di",
            'bpmnElement': lane['id'],
            'isHorizontal': 'true'
        })
        ET.SubElement(shape, 'dc:Bounds', {
            'x': '100', 'y': str(cumulative_y), 'width': str(lane_width), 'height': str(lane_h)
        })
        cumulative_y += lane_h
else:
    cumulative_y = 80
    for lane in lanes:
        lane_h = lane_heights[lane['id']]
        shape = ET.SubElement(plane, 'bpmndi:BPMNShape', {
            'id': f'Participant_{lane["id"]}_di',
            'bpmnElement': f'Participant_{lane["id"]}',
            'isHorizontal': 'true'
        })
        ET.SubElement(shape, 'dc:Bounds', {
            'x': '100', 'y': str(cumulative_y), 'width': str(lane_width), 'height': str(lane_h)
        })
        cumulative_y += lane_h + pool_spacing

# Shapes dos nós
for node_id, pos in positions.items():
    shape = ET.SubElement(plane, 'bpmndi:BPMNShape', {
        'id': f"{node_id}_di", 'bpmnElement': node_id
    })
    ET.SubElement(shape, 'dc:Bounds', {
        'x': str(int(pos['x'])), 'y': str(int(pos['y'])),
        'width': str(int(pos['width'])), 'height': str(int(pos['height']))
    })

# Edges
edges_by_source = defaultdict(list)
edges_by_target = defaultdict(list)

for edge in data['edges']:
    edges_by_source[edge['source']].append(edge)
    edges_by_target[edge['target']].append(edge)

# Sequence flows
for edge in sequence_flows:
    fid = f"Flow_{edge['source']}_{edge['target']}"
    src_id, tgt_id = edge['source'], edge['target']

    if src_id in positions and tgt_id in positions:
        edge_elem = ET.SubElement(plane, 'bpmndi:BPMNEdge', {'id': f"{fid}_di", 'bpmnElement': fid})

        offset = 0
        source_siblings = edges_by_source[src_id]
        if len(source_siblings) > 1:
            src_idx = source_siblings.index(edge)
            offset += -((len(source_siblings) - 1) * 15) / 2 + (src_idx * 15)

        target_siblings = edges_by_target[tgt_id]
        if len(target_siblings) > 1:
            tgt_idx = target_siblings.index(edge)
            offset += -((len(target_siblings) - 1) * 10) / 2 + (tgt_idx * 10)

        waypoints = create_waypoints_with_ports(positions[src_id], positions[tgt_id], offset)
        for x, y in waypoints:
            ET.SubElement(edge_elem, 'di:waypoint', {'x': str(int(x)), 'y': str(int(y))})

# Message flows
for edge in message_flows:
    fid = f"MessageFlow_{edge['source']}_{edge['target']}"
    src_id, tgt_id = edge['source'], edge['target']

    if src_id in positions and tgt_id in positions:
        edge_elem = ET.SubElement(plane, 'bpmndi:BPMNEdge', {'id': f"{fid}_di", 'bpmnElement': fid})

        offset = 0
        source_siblings = edges_by_source[src_id]
        if len(source_siblings) > 1:
            src_idx = source_siblings.index(edge)
            offset += -((len(source_siblings) - 1) * 15) / 2 + (src_idx * 15)

        target_siblings = edges_by_target[tgt_id]
        if len(target_siblings) > 1:
            tgt_idx = target_siblings.index(edge)
            offset += -((len(target_siblings) - 1) * 10) / 2 + (tgt_idx * 10)

        waypoints = create_waypoints_with_ports(positions[src_id], positions[tgt_id], offset)
        for x, y in waypoints:
            ET.SubElement(edge_elem, 'di:waypoint', {'x': str(int(x)), 'y': str(int(y))})

tree = ET.ElementTree(bpmn)
ET.indent(tree, space="  ")
tree.write(name, encoding="utf-8", xml_declaration=True)

print("\n✅ Arquivo salvo com sucesso!")
print(f"🏊 {len(lanes)} raias | {len(data['nodes'])} nós | {len(data['edges'])} arestas")
print(f"📨 {len(message_flows)} message flows (linhas pontilhadas)")
print(f"➡️ {len(sequence_flows)} sequence flows (linhas normais)")
print(f"🔧 Modo: {'Lanes no mesmo pool' if process == 'y' else 'Pools independentes'}")
print("📂 Abra em: https://demo.bpmn.io/new")
print("\n" + "="*60)
print("🎯 CONFIGURAÇÃO ATUAL:")
print("="*60)
print(f"process = '{process}' ({'Lanes no mesmo pool' if process == 'y' else 'Pools independentes'})")
print(f"raias = '{raias}'")
if process == 'n':
    print("⚠️  Validação estrutural ATIVA - nós intermediários adicionados automaticamente")
else:
    print("ℹ️  Validação estrutural DESATIVADA - sequence flows diretos permitidos")
print("="*60)
