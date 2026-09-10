
import speech_recognition as sr
import subprocess
import os
import json
import threading
import queue
import logging
import webbrowser
import datetime
import asyncio
import ollama
import psutil
import edge_tts
import pygame
import pystray
import pyaudio
import numpy as np
from openwakeword.model import Model as WakeWordModel
from PIL import Image, ImageDraw
from plyer import notification

from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

MODELO_LLM = "llama3.2"
VOZ_TTS = "pt-BR-AntonioNeural"
ARQUIVO_AUDIO_TEMP = "jarvis_fala.mp3"
ARQUIVO_MEMORIA = "jarvis_memoria.json"
ARQUIVO_LOG = "jarvis_log.txt"
PALAVRA_ATIVACAO = "claude"

# Wake word local (detecção roda no PC, sem precisar da nuvem)
ARQUIVO_MODELO_WAKE_WORD = "claude.onnx"  # gerado no treino, ver instruções
LIMIAR_WAKE_WORD = 0.5  # quanto menor, mais sensível (e mais falso positivo)
TAXA_AMOSTRAGEM = 16000
TAMANHO_CHUNK = 1280  # ~80ms de áudio por vez, é o que o openWakeWord espera
ACOES_DESTRUTIVAS = {"desligar_pc", "reiniciar_pc"}
PALAVRAS_CONFIRMACAO = {"sim", "confirmo", "confirmado", "pode", "isso"}
MAX_FATOS_MEMORIA = 20

PALAVRAS_CHAVE_DESTRUTIVAS = {
    "desligar_pc": [("desliga", "computador"), ("desliga", "pc"), ("desligar", "computador"), ("desligar", "pc")],
    "reiniciar_pc": [("reinicia", "computador"), ("reinicia", "pc"), ("reiniciar", "computador"), ("reiniciar", "pc")],
}


def frase_bate_com_acao_destrutiva(nome_acao, frase_original):
    """Segunda checagem, baseada em regra fixa (não em LLM), antes de
    aceitar uma ação destrutiva. Reduz falso positivo por alucinação do
    modelo — se a frase falada nem menciona as palavras esperadas, a
    ação é barrada mesmo que o LLM tenha decidido executá-la."""
    combinacoes = PALAVRAS_CHAVE_DESTRUTIVAS.get(nome_acao, [])
    return any(p1 in frase_original and p2 in frase_original for p1, p2 in combinacoes)

# Log de ações
logging.basicConfig(
    filename=ARQUIVO_LOG,
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def registrar_log(mensagem):
    logging.info(mensagem)


# Memória persistente

def carregar_memoria():
    if not os.path.exists(ARQUIVO_MEMORIA):
        return []
    with open(ARQUIVO_MEMORIA, "r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def salvar_memoria(memoria):
    with open(ARQUIVO_MEMORIA, "w", encoding="utf-8") as arquivo:
        json.dump(memoria, arquivo, ensure_ascii=False, indent=2)


memoria_fatos = carregar_memoria()

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

PALAVRAS_SAIDA = {"parar", "encerrar", "sair", "desligar o jarvis"}

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


def montar_prompt_sistema():
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


# Execução de cada ação-

def abrir_programa(programa, **_):
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


def fechar_programa(programa, **_):
    nome_processo = PROCESSOS.get(programa)
    if not nome_processo:
        return False
    encontrou = False
    for processo in psutil.process_iter(["name"]):
        if processo.info["name"] and processo.info["name"].lower() == nome_processo.lower():
            processo.terminate()
            encontrou = True
    return encontrou


def pesquisar_web(termo, **_):
    url = f"https://www.google.com/search?q={termo.replace(' ', '+')}"
    webbrowser.open(url)
    return True


def _pegar_volume_endpoint():
    dispositivos = AudioUtilities.GetSpeakers()
    interface = dispositivos.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def controlar_volume(tipo, **_):
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


def dizer_hora(**_):
    agora = datetime.datetime.now()
    return agora.strftime("Agora são %H:%M de %d/%m/%Y")


def desligar_pc(**_):
    subprocess.Popen(["shutdown", "/s", "/t", "0"])
    return True


def reiniciar_pc(**_):
    subprocess.Popen(["shutdown", "/r", "/t", "0"])
    return True


def criar_lembrete(minutos, mensagem, **_):
    segundos = float(minutos) * 60

    def avisar():
        falar(f"Lembrete: {mensagem}")

    threading.Timer(segundos, avisar).start()
    return True


def lembrar_fato(fato, **_):
    memoria_fatos.append(fato)
    _condensar_memoria_se_necessario()
    salvar_memoria(memoria_fatos)
    return True


def _condensar_memoria_se_necessario():
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
        )
        resumo = resposta["message"]["content"].strip()
    except Exception as erro:
        registrar_log(f"Falha ao condensar memória: {erro}")
        resumo = "; ".join(fatos_antigos)  # não perde a informação mesmo se o resumo falhar

    memoria_fatos = [f"(resumo de fatos antigos) {resumo}"] + fatos_recentes
    registrar_log(f"Memória condensada: {len(fatos_antigos)} fatos antigos viraram 1 resumo.")


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


# Voz 

async def _gerar_audio(texto):
    comunicador = edge_tts.Communicate(texto, voice=VOZ_TTS)
    await comunicador.save(ARQUIVO_AUDIO_TEMP)


def falar(texto, fila_interrupcao=None):
    """Fala o texto. Se `fila_interrupcao` receber algo enquanto fala,
    para na hora (permite interromper com 'claude' de novo)."""
    print(f"[JARVIS] {texto}")

    notification.notify(title="JARVIS", message=texto, timeout=5, app_name="JARVIS")

    asyncio.run(_gerar_audio(texto))

    pygame.mixer.music.load(ARQUIVO_AUDIO_TEMP)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        if fila_interrupcao is not None and not fila_interrupcao.empty():
            pygame.mixer.music.stop()
            break
        pygame.time.wait(100)

    pygame.mixer.music.unload()
    os.remove(ARQUIVO_AUDIO_TEMP)


# Cérebro (LLM) 

def perguntar_ao_cerebro(texto_usuario, historico, tentativas=2):
    historico.append({"role": "user", "content": texto_usuario})

    for tentativa in range(tentativas):
        resposta = ollama.chat(
            model=MODELO_LLM,
            messages=[{"role": "system", "content": montar_prompt_sistema()}] + historico,
            format="json",
            options={"temperature": 0},  # menos "criatividade", mais consistência na decisão
        )
        conteudo = resposta["message"]["content"]

        try:
            decisao = json.loads(conteudo)
            historico.append({"role": "assistant", "content": conteudo})
            return decisao
        except json.JSONDecodeError:
            registrar_log(f"JSON inválido do LLM (tentativa {tentativa + 1}): {conteudo}")
            # pede de novo, reforçando o formato
            historico.append({"role": "user", "content": "Responda APENAS o JSON válido, sem mais nada."})

    return {"acao": "conversar", "parametros": {}, "resposta": "Não entendi direito, pode repetir?"}


def executar_acao(decisao):
    nome_acao = decisao.get("acao")
    parametros = decisao.get("parametros", {}) or {}

    registrar_log(f"Ação decidida: {nome_acao} | parâmetros: {parametros}")

    if nome_acao == "dizer_hora":
        return dizer_hora()

    if nome_acao == "conversar" or nome_acao not in EXECUTORES:
        return None

    funcao = EXECUTORES[nome_acao]
    try:
        sucesso = funcao(**parametros)
        resultado = None if sucesso else f"Não consegui completar a ação {nome_acao}."
        registrar_log(f"Resultado de {nome_acao}: {'sucesso' if sucesso else 'falhou'}")
        return resultado
    except Exception as erro:
        registrar_log(f"Erro em {nome_acao}: {erro}")
        return f"Deu erro tentando fazer isso: {erro}"


# Escuta em thread separada 

def _captar_comando_google():
    """Só é chamado DEPOIS que a wake word já disparou localmente — ou seja,
    a nuvem só recebe áudio quando o usuário realmente chamou o JARVIS."""
    reconhecedor = sr.Recognizer()
    with sr.Microphone() as fonte:
        reconhecedor.adjust_for_ambient_noise(fonte, duration=0.5)
        try:
            audio = reconhecedor.listen(fonte, timeout=5, phrase_time_limit=6)
            return reconhecedor.recognize_google(audio, language="pt-BR")
        except (sr.WaitTimeoutError, sr.UnknownValueError, sr.RequestError):
            return None


def thread_escuta(fila_comandos, parar_evento, estado_confirmacao):
    """A detecção da wake word roda 100% local (openWakeWord), processando
    o áudio em pedacinhos de ~80ms direto no seu PC. Só quando o score passa
    do limiar é que a gente abre o microfone pro reconhecimento por nuvem
    (Google), pra capturar o comando em si. É essa troca que evita mandar
    toda fala do ambiente pra internet."""
    wake_model = WakeWordModel(wakeword_models=[ARQUIVO_MODELO_WAKE_WORD])
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=TAXA_AMOSTRAGEM,
        input=True,
        frames_per_buffer=TAMANHO_CHUNK,
    )

    try:
        while not parar_evento.is_set():
            if estado_confirmacao["aguardando"]:
                # durante uma confirmação (sim/não), escuta direto por nuvem —
                # é rápido e raro, não vale a pena complicar com wake word aqui
                resposta = _captar_comando_google()
                if resposta:
                    fila_comandos.put(resposta.lower())
                continue

            chunk = stream.read(TAMANHO_CHUNK, exception_on_overflow=False)
            chunk_np = np.frombuffer(chunk, dtype=np.int16)
            predicoes = wake_model.predict(chunk_np)
            pontuacao = predicoes.get(ARQUIVO_MODELO_WAKE_WORD.replace(".onnx", ""), 0)

            if pontuacao > LIMIAR_WAKE_WORD:
                registrar_log(f"Wake word detectada localmente (score {pontuacao:.2f}).")
                stream.stop_stream()
                comando = _captar_comando_google()
                stream.start_stream()
                fila_comandos.put((comando or "").lower())
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


# Loop principal (roda dentro da bandeja)

def loop_principal(icone):
    icone.visible = True
    pygame.mixer.init()

    fila_comandos = queue.Queue()
    parar_evento = threading.Event()
    estado_confirmacao = {"aguardando": False}

    escuta = threading.Thread(
        target=thread_escuta, args=(fila_comandos, parar_evento, estado_confirmacao), daemon=True
    )
    escuta.start()

    historico = []
    registrar_log("JARVIS iniciado.")
    falar(f'JARVIS em espera. Diga "{PALAVRA_ATIVACAO}" pra me chamar.')

    while not parar_evento.is_set():
        try:
            comando = fila_comandos.get(timeout=1)
        except queue.Empty:
            continue

        if not comando:
            falar("Diga.", fila_interrupcao=fila_comandos)
            try:
                comando = fila_comandos.get(timeout=8)
            except queue.Empty:
                continue

        if any(palavra in comando for palavra in PALAVRAS_SAIDA):
            falar("Até mais.")
            registrar_log("JARVIS encerrado por comando de voz.")
            parar_evento.set()
            icone.stop()
            break

        decisao = perguntar_ao_cerebro(comando, historico)
        nome_acao = decisao.get("acao")

        if nome_acao in ACOES_DESTRUTIVAS:
            if not frase_bate_com_acao_destrutiva(nome_acao, comando):
                registrar_log(
                    f"Ação destrutiva '{nome_acao}' bloqueada: frase '{comando}' não bate com as palavras-chave esperadas."
                )
                falar("Não tenho certeza que foi isso que você pediu, então não vou executar.", fila_interrupcao=fila_comandos)
                continue

            falar(
                f'Tem certeza que quer que eu execute "{nome_acao}"? Diga "sim" pra confirmar.',
                fila_interrupcao=fila_comandos,
            )
            estado_confirmacao["aguardando"] = True
            try:
                resposta = fila_comandos.get(timeout=8).strip()
            except queue.Empty:
                resposta = ""
            estado_confirmacao["aguardando"] = False

            if resposta not in PALAVRAS_CONFIRMACAO:
                falar("Ação cancelada.", fila_interrupcao=fila_comandos)
                registrar_log(f"Ação destrutiva '{nome_acao}' cancelada (sem confirmação).")
                continue

            registrar_log(f"Ação destrutiva '{nome_acao}' confirmada pelo usuário.")

        erro_execucao = executar_acao(decisao)

        if nome_acao == "dizer_hora":
            falar(dizer_hora(), fila_interrupcao=fila_comandos)
        elif erro_execucao:
            falar(erro_execucao, fila_interrupcao=fila_comandos)
        else:
            falar(decisao.get("resposta", "Feito."), fila_interrupcao=fila_comandos)

        if len(historico) > 12:
            del historico[:2]


def encerrar_pelo_tray(icone, item):
    registrar_log("JARVIS encerrado pela bandeja do sistema.")
    icone.stop()
    os._exit(0)


def criar_icone():
    imagem = Image.new("RGB", (64, 64), "black")
    desenho = ImageDraw.Draw(imagem)
    desenho.ellipse((8, 8, 56, 56), fill="deepskyblue")
    return imagem


def main():
    menu = pystray.Menu(pystray.MenuItem("Encerrar JARVIS", encerrar_pelo_tray))
    icone = pystray.Icon("jarvis", criar_icone(), "JARVIS", menu)
    icone.run(setup=loop_principal)


if __name__ == "__main__":
    main()
