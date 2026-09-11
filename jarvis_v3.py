"""
JARVIS - Assistente pessoal por voz (v4)
=========================================

Principais mudanças em relação às versões anteriores:

1. Áudio num único stream contínuo. A wake word e a captura do comando
   usam o MESMO stream do início ao fim — nunca fecha e reabre o
   microfone. Isso elimina os cliques/ecos que causavam disparos
   duplicados da wake word logo depois de um comando.

2. Transcrição local com faster-whisper, no lugar do Google Speech
   Recognition gratuito. Roda offline (não depende da internet nem de
   limite de uso de uma API de terceiros) e é bem mais preciso em
   português. Tenta usar a GPU (RTX 3050); se não conseguir, cai pra CPU
   sozinho, sem travar o programa.

3. Rota rápida sem LLM. Comandos comuns e inequívocos (abrir/fechar
   programa, hora, volume) são resolvidos na hora, sem esperar o Ollama
   pensar. O LLM só é chamado pra conversa livre ou pedidos ambíguos.

4. Recuperação automática de erros. Um erro pontual (glitch de áudio,
   Ollama fora do ar, falha na transcrição) não derruba a thread de
   escuta pro resto da sessão — ela loga o problema e continua.
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from ctypes import POINTER, cast

# No Windows, o faster-whisper (via CTranslate2) procura as DLLs de CUDA
# (cuBLAS, cuDNN, CUDA Runtime) nas pastas do sistema/PATH. Quando elas
# vêm instaladas via pip (pacotes nvidia-*-cu12) em vez do CUDA Toolkit
# completo, ficam dentro do site-packages e o Windows não acha sozinho —
# por isso registramos essas pastas aqui, ANTES de importar qualquer
# coisa que dependa delas. Usamos os.add_dll_directory() (mecanismo
# moderno) E o PATH clássico (mais universalmente respeitado por
# bibliotecas C++ compiladas), pra cobrir os dois casos.
if sys.platform == "win32":
    import glob
    import site

    _dll_dirs = []
    for _base in site.getsitepackages():
        _dll_dirs.extend(glob.glob(os.path.join(_base, "nvidia", "*", "bin")))

    for _caminho_dll in _dll_dirs:
        os.add_dll_directory(_caminho_dll)

    if _dll_dirs:
        os.environ["PATH"] = os.pathsep.join(_dll_dirs) + os.pathsep + os.environ.get("PATH", "")
        print(f"[JARVIS] Pastas de DLL da NVIDIA registradas: {_dll_dirs}")
    else:
        print("[JARVIS] Nenhuma pasta de DLL da NVIDIA encontrada em site-packages (GPU provavelmente vai cair pra CPU).")

import edge_tts
import numpy as np
import ollama
import psutil
import pyaudio
import pygame
import pystray
from comtypes import CLSCTX_ALL
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeWordModel
from PIL import Image, ImageDraw
from plyer import notification
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# ======================================================================
# Configuração
# ======================================================================

# --- Arquivos ---
ARQUIVO_AUDIO_TEMP = "jarvis_fala.mp3"
ARQUIVO_MEMORIA = "jarvis_memoria.json"
ARQUIVO_LOG = "jarvis_log.txt"

# --- LLM (Ollama) ---
MODELO_LLM = "llama3.2"
OLLAMA_KEEP_ALIVE = "10m"  # mantém o modelo carregado na memória por mais tempo
MAX_FATOS_MEMORIA = 20

# --- Voz (TTS) ---
VOZ_TTS = "pt-BR-AntonioNeural"

# --- Wake word ---
PALAVRA_ATIVACAO = "hey jarvis"
ARQUIVO_MODELO_WAKE_WORD = "hey_jarvis"  # modelo pré-treinado do openWakeWord
LIMIAR_WAKE_WORD = 0.5
FRAMES_CONSECUTIVOS_PARA_CONFIRMAR = 2  # exige 2 chunks (~160ms) seguidos acima do limiar
COOLDOWN_APOS_FALA_SEGUNDOS = 0.6  # margem de segurança após o JARVIS terminar de falar

# --- Áudio ---
TAXA_AMOSTRAGEM = 16000
TAMANHO_CHUNK = 1280  # ~80ms, é o que o openWakeWord espera
SEGUNDOS_CALIBRACAO_RUIDO = 1.0
FATOR_LIMIAR_SILENCIO = 2.5  # múltiplo do ruído ambiente pra considerar "silêncio"
PISO_LIMIAR_SILENCIO = 150.0  # nunca considera silêncio abaixo disso (RMS bruto)
SILENCIO_PARA_ENCERRAR_SEGUNDOS = 1.0
DURACAO_MINIMA_COMANDO_SEGUNDOS = 0.3
DURACAO_MAXIMA_COMANDO_SEGUNDOS = 8.0

# --- STT (faster-whisper) ---
TAMANHO_MODELO_STT = "small"  # tiny/base/small/medium — maior = mais preciso e mais lento
IDIOMA_STT = "pt"

# --- Ações ---
ACOES_DESTRUTIVAS = {"desligar_pc", "reiniciar_pc"}
PALAVRAS_CONFIRMACAO = {"sim", "confirmo", "confirmado", "pode", "isso"}
PALAVRAS_SAIDA = {"parar", "encerrar", "sair", "desligar o jarvis"}

PALAVRAS_CHAVE_DESTRUTIVAS = {
    "desligar_pc": [("desliga", "computador"), ("desliga", "pc"), ("desligar", "computador"), ("desligar", "pc")],
    "reiniciar_pc": [("reinicia", "computador"), ("reinicia", "pc"), ("reiniciar", "computador"), ("reiniciar", "pc")],
}

# Programas que o JARVIS pode abrir/fechar
COMANDOS = {
    "brave": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "steam": r"C:\Program Files (x86)\Steam\steam.exe",
    "bloco_de_notas": "notepad.exe",
    "calculadora": "calc.exe",
    "explorador_de_arquivos": "explorer.exe",
    "vscode": r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "spotify": r"C:\Users\%USERNAME%\AppData\Roaming\Spotify\Spotify.exe",
}

PROCESSOS = {
    "brave": "brave.exe",
    "steam": "steam.exe",
    "bloco_de_notas": "notepad.exe",
    "calculadora": "CalculatorApp.exe",
    "vscode": "Code.exe",
    "spotify": "Spotify.exe",
}

ACOES_DISPONIVEIS = """
- abrir_programa: {"programa": "<um destes: %s>"}
- fechar_programa: {"programa": "<um destes: %s>"}
- pesquisar_web: {"termo": "<o que pesquisar>"}
- controlar_volume: {"tipo": "aumentar" | "diminuir" | "mudo"}
- dizer_hora: {}
- criar_lembrete: {"minutos": <número>, "mensagem": "<o que lembrar>"}
- desligar_pc: {}
- reiniciar_pc: {}
- lembrar_fato: {"fato": "<algo sobre o usuário que ele pediu pra você lembrar>"}
- conversar: {} (quando não é nenhuma das ações acima, é só bate-papo/pergunta)
""" % (list(COMANDOS.keys()), list(COMANDOS.keys()))


# ======================================================================
# Logging
# ======================================================================
# Um único logger pra tudo: grava no arquivo (nível INFO+, sem poluir com
# detalhe de diagnóstico) e mostra no terminal em tempo real (nível DEBUG+,
# útil pra acompanhar o que está acontecendo enquanto testa).

logger = logging.getLogger("jarvis")
logger.setLevel(logging.DEBUG)

_handler_arquivo = logging.FileHandler(ARQUIVO_LOG, encoding="utf-8")
_handler_arquivo.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_handler_arquivo.setLevel(logging.INFO)

_handler_console = logging.StreamHandler()
_handler_console.setFormatter(logging.Formatter("[JARVIS] %(message)s"))
_handler_console.setLevel(logging.DEBUG)

logger.addHandler(_handler_arquivo)
logger.addHandler(_handler_console)


# ======================================================================
# Estado compartilhado entre a thread de escuta e o loop principal
# ======================================================================

class EstadoAssistente:
    """Tudo que a thread de escuta e o loop principal precisam combinar
    entre si. Concentrar isso numa classe evita ficar passando meia dúzia
    de parâmetros soltos pra cada função."""

    def __init__(self) -> None:
        self.fila_comandos: queue.Queue[str] = queue.Queue()
        self.parar = threading.Event()
        self.falando = threading.Event()
        # Ativado quando o loop principal precisa de UMA captura direta,
        # sem exigir a wake word de novo — usado tanto pro "Diga." (quando
        # a wake word disparou mas nada foi entendido) quanto pra esperar
        # a confirmação de uma ação destrutiva.
        self.escuta_direta = threading.Event()
        self.historico: list[dict] = []


# Referência global pro estado, usada só pelo callback assíncrono dos
# lembretes (threading.Timer), que dispara fora do loop principal e por
# isso não recebe o estado como parâmetro normalmente.
_estado_global: EstadoAssistente | None = None


# ======================================================================
# Memória persistente (fatos sobre o usuário)
# ======================================================================

def carregar_memoria() -> list[str]:
    if not os.path.exists(ARQUIVO_MEMORIA):
        return []
    with open(ARQUIVO_MEMORIA, "r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def salvar_memoria(memoria: list[str]) -> None:
    with open(ARQUIVO_MEMORIA, "w", encoding="utf-8") as arquivo:
        json.dump(memoria, arquivo, ensure_ascii=False, indent=2)


memoria_fatos: list[str] = carregar_memoria()


def montar_prompt_sistema() -> str:
    fatos = "\n".join(f"- {fato}" for fato in memoria_fatos) or "(nada ainda)"
    return f"""Você é JARVIS, um assistente pessoal por voz, direto e educado.
Você SEMPRE responde em JSON puro, sem texto fora do JSON, no formato:

{{"acao": "<nome_da_acao>", "parametros": {{...}}, "resposta": "frase curta falando pro usuário"}}

Ações disponíveis e seus parâmetros:
{ACOES_DISPONIVEIS}

Exemplos de como responder corretamente:

Usuário: "abre o spotify"
{{"acao": "abrir_programa", "parametros": {{"programa": "spotify"}}, "resposta": "Abrindo o Spotify."}}

Usuário: "que horas são"
{{"acao": "dizer_hora", "parametros": {{}}, "resposta": ""}}

Usuário: "desliga o pc"
{{"acao": "desligar_pc", "parametros": {{}}, "resposta": "Desligando o computador."}}

Usuário: "pesquisa receita de bolo"
{{"acao": "pesquisar_web", "parametros": {{"termo": "receita de bolo"}}, "resposta": "Pesquisando receita de bolo."}}

Usuário: "qual a capital da frança"
{{"acao": "conversar", "parametros": {{}}, "resposta": "A capital da França é Paris."}}

Fatos que você já sabe sobre o usuário (use pra personalizar respostas):
{fatos}

Se o usuário pedir algo fora dessas ações, use "acao": "conversar" e responda normalmente
no campo "resposta". Se ele pedir pra você lembrar de algo sobre ele, use "lembrar_fato".
NUNCA escolha "desligar_pc" ou "reiniciar_pc" a menos que o usuário tenha pedido isso
de forma clara e explícita.
"""


def frase_bate_com_acao_destrutiva(nome_acao: str, frase_original: str) -> bool:
    """Segunda checagem, baseada em regra fixa (não em LLM), antes de
    aceitar uma ação destrutiva. Reduz falso positivo por alucinação do
    modelo — se a frase falada nem menciona as palavras esperadas, a
    ação é barrada mesmo que o LLM tenha decidido executá-la."""
    combinacoes = PALAVRAS_CHAVE_DESTRUTIVAS.get(nome_acao, [])
    return any(p1 in frase_original and p2 in frase_original for p1, p2 in combinacoes)


# ======================================================================
# Execução de cada ação
# ======================================================================

def _resolver_programa(texto: str) -> str | None:
    """Casa um texto reconhecido por voz (que pode vir com pequenos erros
    de transcrição) com uma chave conhecida em COMANDOS, usando
    correspondência aproximada. Retorna None se não achar nada parecido
    o suficiente."""
    texto = normalizar(texto).strip().replace(" ", "_")
    if not texto:
        return None
    if texto in COMANDOS:
        return texto
    for chave in COMANDOS:
        if chave in texto or texto in chave:
            return chave
    for token in texto.split("_"):
        candidatos = difflib.get_close_matches(token, COMANDOS.keys(), n=1, cutoff=0.6)
        if candidatos:
            return candidatos[0]
    return None


def abrir_programa(programa: str, **_) -> bool:
    caminho = COMANDOS.get(programa)
    if not caminho:
        return False
    caminho_expandido = os.path.expandvars(caminho)
    try:
        if os.sep not in caminho_expandido:
            subprocess.Popen(caminho_expandido, shell=True)
        else:
            subprocess.Popen([caminho_expandido])
        return True
    except (FileNotFoundError, OSError):
        return False


def fechar_programa(programa: str, **_) -> bool:
    nome_processo = PROCESSOS.get(programa)
    if not nome_processo:
        return False
    encontrou = False
    for processo in psutil.process_iter(["name"]):
        if processo.info["name"] and processo.info["name"].lower() == nome_processo.lower():
            processo.terminate()
            encontrou = True
    return encontrou


def pesquisar_web(termo: str, **_) -> bool:
    url = f"https://www.google.com/search?q={termo.replace(' ', '+')}"
    webbrowser.open(url)
    return True


def _pegar_volume_endpoint():
    dispositivos = AudioUtilities.GetSpeakers()
    interface = dispositivos.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def controlar_volume(tipo: str, **_) -> bool:
    volume = _pegar_volume_endpoint()
    if tipo == "mudo":
        volume.SetMute(1, None)
    elif tipo == "aumentar":
        volume.SetMute(0, None)
        atual = volume.GetMasterVolumeLevelScalar()
        volume.SetMasterVolumeLevelScalar(min(atual + 0.1, 1.0), None)
    elif tipo == "diminuir":
        atual = volume.GetMasterVolumeLevelScalar()
        volume.SetMasterVolumeLevelScalar(max(atual - 0.1, 0.0), None)
    return True


def dizer_hora(**_) -> str:
    agora = datetime.datetime.now()
    return agora.strftime("Agora são %H:%M de %d/%m/%Y")


def desligar_pc(**_) -> bool:
    subprocess.Popen(["shutdown", "/s", "/t", "0"])
    return True


def reiniciar_pc(**_) -> bool:
    subprocess.Popen(["shutdown", "/r", "/t", "0"])
    return True


def criar_lembrete(minutos: float, mensagem: str, **_) -> bool:
    segundos = float(minutos) * 60

    def avisar():
        falar(f"Lembrete: {mensagem}", estado=_estado_global)

    threading.Timer(segundos, avisar).start()
    return True


def lembrar_fato(fato: str, **_) -> bool:
    memoria_fatos.append(fato)
    _condensar_memoria_se_necessario()
    salvar_memoria(memoria_fatos)
    return True


def _condensar_memoria_se_necessario() -> None:
    """Isso NÃO é a IA 'aprendendo' de verdade — é só compressão de texto.
    Sem isso, a lista de fatos cresceria pra sempre, deixando o prompt do
    LLM cada vez maior, mais lento e mais caro de processar."""
    global memoria_fatos
    if len(memoria_fatos) <= MAX_FATOS_MEMORIA:
        return

    fatos_recentes = memoria_fatos[-10:]
    fatos_antigos = memoria_fatos[:-10]
    texto_antigos = "\n".join(f"- {f}" for f in fatos_antigos)

    try:
        resposta = ollama.chat(
            model=MODELO_LLM,
            messages=[{
                "role": "user",
                "content": (
                    "Resuma estes fatos sobre uma pessoa em até 5 frases curtas, "
                    "sem perder nenhuma informação importante:\n" + texto_antigos
                ),
            }],
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
        resumo = resposta["message"]["content"].strip()
    except Exception as erro:
        logger.error(f"Falha ao condensar memória: {erro}")
        resumo = "; ".join(fatos_antigos)  # não perde a informação mesmo se o resumo falhar

    memoria_fatos = [f"(resumo de fatos antigos) {resumo}"] + fatos_recentes
    logger.info(f"Memória condensada: {len(fatos_antigos)} fatos antigos viraram 1 resumo.")


EXECUTORES = {
    "abrir_programa": abrir_programa,
    "fechar_programa": fechar_programa,
    "pesquisar_web": pesquisar_web,
    "controlar_volume": controlar_volume,
    "criar_lembrete": criar_lembrete,
    "desligar_pc": desligar_pc,
    "reiniciar_pc": reiniciar_pc,
    "lembrar_fato": lembrar_fato,
}


def executar_acao(decisao: dict) -> str | None:
    nome_acao = decisao.get("acao")
    parametros = decisao.get("parametros", {}) or {}

    logger.info(f"Ação decidida: {nome_acao} | parâmetros: {parametros}")

    if nome_acao == "dizer_hora":
        return dizer_hora()

    if nome_acao == "conversar" or nome_acao not in EXECUTORES:
        return None

    funcao = EXECUTORES[nome_acao]
    try:
        sucesso = funcao(**parametros)
        resultado = None if sucesso else f"Não consegui completar a ação {nome_acao}."
        logger.info(f"Resultado de {nome_acao}: {'sucesso' if sucesso else 'falhou'}")
        return resultado
    except Exception as erro:
        logger.error(f"Erro em {nome_acao}: {erro}")
        return f"Deu erro tentando fazer isso: {erro}"


# ======================================================================
# Rota rápida — resolve comandos comuns sem chamar o LLM
# ======================================================================
# Cobre só o que dá pra reconhecer com confiança; qualquer coisa ambígua
# ou fora desse conjunto cai pro LLM normalmente. O ganho é velocidade:
# essas respostas saem quase na hora, sem esperar o Ollama gerar nada.

_PADRAO_ABRIR = re.compile(r"\b(?:abre|abrir|abra)\b\s+(?:o|a)?\s*([a-z0-9_ ]+)")
_PADRAO_FECHAR = re.compile(r"\b(?:fecha|fechar|feche)\b\s+(?:o|a)?\s*([a-z0-9_ ]+)")


def normalizar(texto: str) -> str:
    """Minúsculas e sem acentuação — facilita casar padrões vindos da
    transcrição, que pode vir com ou sem pontuação/maiúsculas."""
    texto = texto.strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^\w\s]", "", texto)
    return texto.strip()


def tentar_rota_rapida(comando: str) -> dict | None:
    """Tenta resolver o comando com regras determinísticas. Retorna um
    dicionário no mesmo formato que o LLM devolveria, ou None se não
    reconheceu nada (nesse caso, quem chamou deve cair pro LLM)."""

    if "que horas" in comando or "hora e" in comando:
        return {"acao": "dizer_hora", "parametros": {}, "resposta": ""}

    m = _PADRAO_ABRIR.search(comando)
    if m:
        programa = _resolver_programa(m.group(1))
        if programa:
            nome_falado = programa.replace("_", " ")
            return {"acao": "abrir_programa", "parametros": {"programa": programa}, "resposta": f"Abrindo o {nome_falado}."}

    m = _PADRAO_FECHAR.search(comando)
    if m:
        programa = _resolver_programa(m.group(1))
        if programa:
            nome_falado = programa.replace("_", " ")
            return {"acao": "fechar_programa", "parametros": {"programa": programa}, "resposta": f"Fechando o {nome_falado}."}

    if "aumenta" in comando and "volume" in comando:
        return {"acao": "controlar_volume", "parametros": {"tipo": "aumentar"}, "resposta": "Aumentando o volume."}
    if ("diminui" in comando or "abaixa" in comando) and "volume" in comando:
        return {"acao": "controlar_volume", "parametros": {"tipo": "diminuir"}, "resposta": "Diminuindo o volume."}
    if "mudo" in comando or "silencia" in comando:
        return {"acao": "controlar_volume", "parametros": {"tipo": "mudo"}, "resposta": "Deixando no mudo."}

    return None


# ======================================================================
# Voz (TTS)
# ======================================================================

async def _gerar_audio(texto: str) -> None:
    comunicador = edge_tts.Communicate(texto, voice=VOZ_TTS)
    await comunicador.save(ARQUIVO_AUDIO_TEMP)


def falar(texto: str, estado: EstadoAssistente | None = None) -> None:
    """Sintetiza e reproduz `texto` em voz.

    Enquanto fala, marca `estado.falando` (se um estado for passado) —
    a thread de escuta usa isso pra pausar completamente a avaliação de
    áudio nesse período, evitando que o JARVIS ouça a própria voz e se
    autointerrompa."""
    logger.info(texto)

    try:
        notification.notify(title="JARVIS", message=texto, timeout=5, app_name="JARVIS")
    except Exception as erro:
        logger.debug(f"(debug) Notificação do Windows falhou (não é crítico): {erro}")

    if estado is not None:
        estado.falando.set()
    try:
        asyncio.run(_gerar_audio(texto))
        pygame.mixer.music.load(ARQUIVO_AUDIO_TEMP)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.wait(100)
        pygame.mixer.music.unload()
        os.remove(ARQUIVO_AUDIO_TEMP)
    except Exception as erro:
        logger.error(f"Falha ao gerar/reproduzir a fala: {erro}")
    finally:
        if estado is not None:
            estado.falando.clear()


# ======================================================================
# Cérebro (LLM)
# ======================================================================

def perguntar_ao_cerebro(texto_usuario: str, historico: list[dict], tentativas: int = 2) -> dict:
    historico.append({"role": "user", "content": texto_usuario})

    for tentativa in range(tentativas):
        try:
            resposta = ollama.chat(
                model=MODELO_LLM,
                messages=[{"role": "system", "content": montar_prompt_sistema()}] + historico,
                format="json",
                options={"temperature": 0},  # menos "criatividade", mais consistência na decisão
                keep_alive=OLLAMA_KEEP_ALIVE,
            )
        except Exception as erro:
            logger.error(f"Não consegui falar com o Ollama: {erro}")
            return {
                "acao": "conversar",
                "parametros": {},
                "resposta": "Não consegui pensar direito agora, meu cérebro parece estar fora do ar.",
            }

        conteudo = resposta["message"]["content"]
        try:
            decisao = json.loads(conteudo)
            historico.append({"role": "assistant", "content": conteudo})
            return decisao
        except json.JSONDecodeError:
            logger.warning(f"JSON inválido do LLM (tentativa {tentativa + 1}): {conteudo}")
            historico.append({"role": "user", "content": "Responda APENAS o JSON válido, sem mais nada."})

    return {"acao": "conversar", "parametros": {}, "resposta": "Não entendi direito, pode repetir?"}


# ======================================================================
# Transcrição local (faster-whisper)
# ======================================================================

def _testar_modelo_stt(modelo: WhisperModel) -> None:
    """Roda uma transcrição mínima logo após carregar o modelo, pra
    garantir que ele REALMENTE funciona nesse dispositivo. Carregar sem
    erro não é garantia — problemas como DLL de CUDA faltando só
    aparecem na hora de uma inferência de verdade, não no carregamento."""
    audio_silencio = np.zeros(TAXA_AMOSTRAGEM, dtype=np.float32)  # 1s de silêncio
    segmentos, _info = modelo.transcribe(audio_silencio, language=IDIOMA_STT, beam_size=1)
    list(segmentos)  # força a geração (é um gerador preguiçoso, sem isso não roda de verdade)


def carregar_modelo_stt() -> WhisperModel:
    """Tenta carregar o modelo na GPU (bem mais rápido) e testa com uma
    inferência real; se não der certo (driver CUDA ausente, cuDNN/cuBLAS
    faltando, etc.), cai pra CPU sozinho."""
    try:
        modelo = WhisperModel(TAMANHO_MODELO_STT, device="cuda", compute_type="float16")
        _testar_modelo_stt(modelo)
        logger.info(f"Modelo de transcrição '{TAMANHO_MODELO_STT}' carregado na GPU.")
        return modelo
    except Exception as erro:
        logger.warning(f"Não consegui usar a GPU pra transcrição ({erro}); usando CPU.")
        modelo = WhisperModel(TAMANHO_MODELO_STT, device="cpu", compute_type="int8")
        logger.info(f"Modelo de transcrição '{TAMANHO_MODELO_STT}' carregado na CPU.")
        return modelo


def transcrever(modelo_stt: WhisperModel, audio_int16: np.ndarray) -> str:
    if audio_int16.size == 0:
        return ""
    audio_float = audio_int16.astype(np.float32) / 32768.0
    segmentos, _info = modelo_stt.transcribe(
        audio_float,
        language=IDIOMA_STT,
        beam_size=1,                     # prioriza velocidade
        vad_filter=True,                 # o próprio whisper filtra silêncio/ruído residual
        condition_on_previous_text=False,  # evita repetir/alucinar texto de contexto anterior
    )
    return " ".join(segmento.text for segmento in segmentos).strip()


# ======================================================================
# Áudio: calibração de ruído e captura de comando (mesmo stream sempre)
# ======================================================================

def _rms(amostra: np.ndarray) -> float:
    return float(np.sqrt(np.mean(amostra.astype(np.float64) ** 2))) if amostra.size else 0.0


def _calibrar_ruido_ambiente(stream) -> float:
    """Mede o ruído de fundo por um instante, pra adaptar o limiar de
    silêncio usado ao capturar comandos — em vez de um valor fixo que
    funciona bem num quarto silencioso e mal num ambiente barulhento."""
    amostras = []
    n_chunks = max(int(SEGUNDOS_CALIBRACAO_RUIDO * TAXA_AMOSTRAGEM / TAMANHO_CHUNK), 1)
    for _ in range(n_chunks):
        chunk = stream.read(TAMANHO_CHUNK, exception_on_overflow=False)
        amostras.append(np.frombuffer(chunk, dtype=np.int16))
    rms = _rms(np.concatenate(amostras)) if amostras else 0.0
    limiar = max(rms * FATOR_LIMIAR_SILENCIO, PISO_LIMIAR_SILENCIO)
    logger.info(f"Ruído ambiente calibrado (RMS {rms:.0f}); limiar de silêncio definido em {limiar:.0f}.")
    return limiar


def _capturar_comando(stream, limiar_silencio: float) -> np.ndarray:
    """Grava do MESMO stream já aberto (sem reabrir o microfone) até
    detectar silêncio suficiente ou atingir o tempo máximo. Usa energia
    (RMS) pra decidir onde a fala termina, sem depender de nenhum
    serviço externo pra 'endpointing'."""
    pedacos = []
    duracao_chunk = TAMANHO_CHUNK / TAXA_AMOSTRAGEM
    duracao_total = 0.0
    duracao_silencio = 0.0

    while duracao_total < DURACAO_MAXIMA_COMANDO_SEGUNDOS:
        chunk = stream.read(TAMANHO_CHUNK, exception_on_overflow=False)
        pedacos.append(chunk)
        duracao_total += duracao_chunk

        rms = _rms(np.frombuffer(chunk, dtype=np.int16))
        duracao_silencio = duracao_silencio + duracao_chunk if rms < limiar_silencio else 0.0

        if duracao_total >= DURACAO_MINIMA_COMANDO_SEGUNDOS and duracao_silencio >= SILENCIO_PARA_ENCERRAR_SEGUNDOS:
            break

    return np.frombuffer(b"".join(pedacos), dtype=np.int16)


# ======================================================================
# Thread de escuta
# ======================================================================

def thread_escuta(estado: EstadoAssistente, modelo_stt: WhisperModel) -> None:
    """Roda continuamente num único stream de áudio:

    1) avalia a wake word local (openWakeWord) chunk a chunk;
    2) ao confirmar (alguns chunks seguidos acima do limiar), grava o
       comando no MESMO stream até detectar silêncio;
    3) transcreve localmente com faster-whisper e enfileira o texto.

    Fica pausada (sem avaliar nada) enquanto `estado.falando` está
    ativo, com uma pequena margem de segurança depois que a fala
    termina — evita autointerrupção e ecos residuais."""
    wake_model = WakeWordModel(wakeword_models=[ARQUIVO_MODELO_WAKE_WORD], inference_framework="onnx")
    nome_wake_word = ARQUIVO_MODELO_WAKE_WORD.replace(".onnx", "")

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=TAXA_AMOSTRAGEM,
        input=True,
        frames_per_buffer=TAMANHO_CHUNK,
    )

    try:
        limiar_silencio = _calibrar_ruido_ambiente(stream)
        frames_altos_consecutivos = 0
        pausado_ate = 0.0

        while not estado.parar.is_set():
            try:
                if estado.falando.is_set():
                    stream.read(TAMANHO_CHUNK, exception_on_overflow=False)
                    pausado_ate = time.time() + COOLDOWN_APOS_FALA_SEGUNDOS
                    frames_altos_consecutivos = 0
                    continue

                if time.time() < pausado_ate:
                    stream.read(TAMANHO_CHUNK, exception_on_overflow=False)
                    continue

                if estado.escuta_direta.is_set():
                    audio = _capturar_comando(stream, limiar_silencio)
                    texto = normalizar(transcrever(modelo_stt, audio))
                    logger.debug(f"(debug) Captura direta transcrita: {texto!r}")
                    if texto:
                        estado.fila_comandos.put(texto)
                    continue

                chunk = stream.read(TAMANHO_CHUNK, exception_on_overflow=False)
                chunk_np = np.frombuffer(chunk, dtype=np.int16)
                predicoes = wake_model.predict(chunk_np)
                pontuacao = predicoes.get(nome_wake_word, 0.0)

                frames_altos_consecutivos = frames_altos_consecutivos + 1 if pontuacao > LIMIAR_WAKE_WORD else 0

                if frames_altos_consecutivos >= FRAMES_CONSECUTIVOS_PARA_CONFIRMAR:
                    frames_altos_consecutivos = 0
                    logger.debug(f"(debug) Wake word confirmada (score {pontuacao:.2f}). Ouvindo comando...")
                    audio = _capturar_comando(stream, limiar_silencio)
                    texto = normalizar(transcrever(modelo_stt, audio))
                    logger.debug(f"(debug) Comando transcrito: {texto!r}")
                    # Enfileira mesmo vazio: a wake word REALMENTE disparou,
                    # então o usuário merece o "Diga." em vez de silêncio.
                    estado.fila_comandos.put(texto)
            except Exception as erro:
                # Nunca deixa a thread morrer por um erro pontual — loga e
                # continua escutando, em vez de deixar o JARVIS "surdo"
                # pelo resto da sessão.
                logger.error(f"Erro na thread de escuta (recuperando): {erro}")
                time.sleep(0.5)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


def _supervisor_escuta(estado: EstadoAssistente, modelo_stt: WhisperModel) -> None:
    """Reinicia a thread_escuta automaticamente se ela cair por completo
    (ex: falha ao abrir o microfone). Sem isso, um erro fatal na
    inicialização deixaria o JARVIS mudo pro resto da execução."""
    while not estado.parar.is_set():
        try:
            thread_escuta(estado, modelo_stt)
        except Exception as erro:
            logger.error(f"A thread de escuta caiu inesperadamente: {erro}")
        if estado.parar.is_set():
            break
        logger.info("Reiniciando a escuta em 3 segundos...")
        time.sleep(3)


# ======================================================================
# Loop principal (roda dentro da bandeja)
# ======================================================================

def loop_principal(icone: pystray.Icon) -> None:
    global _estado_global

    icone.visible = True
    pygame.mixer.init()

    estado = EstadoAssistente()
    _estado_global = estado

    try:
        ollama.list()
    except Exception as erro:
        logger.warning(f"Não consegui confirmar que o Ollama está acessível: {erro}")

    logger.info("Carregando modelo de transcrição (pode demorar na primeira vez)...")
    modelo_stt = carregar_modelo_stt()

    escuta = threading.Thread(target=_supervisor_escuta, args=(estado, modelo_stt), daemon=True)
    escuta.start()

    logger.info("JARVIS iniciado.")
    falar(f'JARVIS em espera. Diga "{PALAVRA_ATIVACAO}" pra me chamar.', estado=estado)

    while not estado.parar.is_set():
        try:
            comando = estado.fila_comandos.get(timeout=1)
        except queue.Empty:
            continue

        if not comando:
            falar("Diga.", estado=estado)
            estado.escuta_direta.set()
            try:
                comando = estado.fila_comandos.get(timeout=8)
            except queue.Empty:
                comando = ""
            finally:
                estado.escuta_direta.clear()
            if not comando:
                continue

        estado.falando.set()  # pausa a wake word durante todo o processamento + fala
        try:
            if any(palavra in comando for palavra in PALAVRAS_SAIDA):
                falar("Até mais.", estado=estado)
                logger.info("JARVIS encerrado por comando de voz.")
                estado.parar.set()
                icone.stop()
                break

            decisao = tentar_rota_rapida(comando)
            if decisao is not None:
                logger.debug(f"(debug) Resolvido pela rota rápida, sem LLM: {decisao}")
            else:
                decisao = perguntar_ao_cerebro(comando, estado.historico)

            nome_acao = decisao.get("acao")

            if nome_acao in ACOES_DESTRUTIVAS:
                if not frase_bate_com_acao_destrutiva(nome_acao, comando):
                    logger.warning(
                        f"Ação destrutiva '{nome_acao}' bloqueada: frase '{comando}' não bate com as palavras-chave esperadas."
                    )
                    falar("Não tenho certeza que foi isso que você pediu, então não vou executar.", estado=estado)
                    continue

                falar(f'Tem certeza que quer que eu execute "{nome_acao}"? Diga "sim" pra confirmar.', estado=estado)
                estado.escuta_direta.set()
                estado.falando.clear()  # libera a escuta pra captar a confirmação
                try:
                    resposta = estado.fila_comandos.get(timeout=8)
                except queue.Empty:
                    resposta = ""
                finally:
                    estado.escuta_direta.clear()
                    estado.falando.set()

                if resposta not in PALAVRAS_CONFIRMACAO:
                    falar("Ação cancelada.", estado=estado)
                    logger.info(f"Ação destrutiva '{nome_acao}' cancelada (sem confirmação).")
                    continue

                logger.info(f"Ação destrutiva '{nome_acao}' confirmada pelo usuário.")

            erro_execucao = executar_acao(decisao)

            if nome_acao == "dizer_hora":
                falar(dizer_hora(), estado=estado)
            elif erro_execucao:
                falar(erro_execucao, estado=estado)
            else:
                falar(decisao.get("resposta", "Feito."), estado=estado)
        finally:
            estado.falando.clear()

        if len(estado.historico) > 12:
            del estado.historico[:2]


# ======================================================================
# Bandeja do sistema
# ======================================================================

def encerrar_pelo_tray(icone: pystray.Icon, item) -> None:
    logger.info("JARVIS encerrado pela bandeja do sistema.")
    icone.stop()
    os._exit(0)


def criar_icone() -> Image.Image:
    imagem = Image.new("RGB", (64, 64), "black")
    desenho = ImageDraw.Draw(imagem)
    desenho.ellipse((8, 8, 56, 56), fill="deepskyblue")
    return imagem


def main() -> None:
    menu = pystray.Menu(pystray.MenuItem("Encerrar JARVIS", encerrar_pelo_tray))
    icone = pystray.Icon("jarvis", criar_icone(), "JARVIS", menu)
    icone.run(setup=loop_principal)


if __name__ == "__main__":
    main()