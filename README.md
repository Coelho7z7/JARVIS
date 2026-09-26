# JARVIS

Assistente pessoal por voz, em Python, que controla e automatiza tarefas no computador (Windows). Tudo roda localmente: a palavra de ativação, a transcrição e o modelo de linguagem funcionam sem depender de APIs pagas.

## Funcionalidades

- Ativação por voz com a palavra **"Hey Jarvis"** (openWakeWord)
- Transcrição local em português com **faster-whisper** (usa a GPU se houver, senão a CPU)
- Rota rápida para comandos comuns (abrir/fechar programas, hora, volume) sem passar pelo LLM
- Conversa livre e pedidos ambíguos respondidos por um LLM local via **Ollama** (`llama3.2`)
- Respostas faladas com **edge-tts**
- Controle de volume do sistema, ícone na bandeja e notificações
- Memória de fatos entre sessões e registro de atividades em log
- Recuperação automática de erros: uma falha pontual não derruba a escuta

## Tecnologias

Python · faster-whisper · openWakeWord · Ollama · edge-tts · PyAudio · pycaw · pystray

## Requisitos

- Windows
- Python 3.10+
- [Ollama](https://ollama.com) instalado, com o modelo baixado:

  ```bash
  ollama pull llama3.2
  ```

- Microfone
- Opcional: GPU NVIDIA com CUDA, para uma transcrição mais rápida

## Como executar

```bash
git clone https://github.com/Coelho7z7/JARVIS.git
cd JARVIS
pip install -r requirements.txt
python jarvis_v3.py
```

Depois é só dizer **"Hey Jarvis"** e dar o comando.

## Estrutura

```
JARVIS/
├── jarvis_v3.py       # assistente completo
├── requirements.txt   # dependências
└── README.md
```

Os arquivos `jarvis_log.txt`, `jarvis_memoria.json` e `jarvis_fala.mp3` são gerados ao rodar e ficam fora do Git.

## Status

Em desenvolvimento.

## Autor

Desenvolvido por [Matheus Coelho](https://github.com/Coelho7z7).
