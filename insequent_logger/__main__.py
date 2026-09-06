from __future__ import annotations

import argparse

from .config import load_config
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Compact, reconstructable LLM tracing proxy")
    parser.add_argument("--config", default="config.toml", help="TOML configuration path")
    parser.add_argument("--db", help="Override SQLite .llmtrace path")
    parser.add_argument("--host", help="Override listener host")
    parser.add_argument("--port", type=int, help="Override listener port")
    parser.add_argument("--upstream", help="Override upstream base URL")
    args = parser.parse_args()
    config = load_config(args.config)
    reranker = config.get("reranker", {})
    reranker_enabled = bool(reranker.get("enabled", False))
    reranker_llama_cpp = reranker.get("llama_cpp", {})
    reranker_listen = reranker.get("listen", {})
    serve(
        args.db or config["storage"]["path"],
        host=args.host or config["server"]["host"],
        port=args.port or int(config["server"]["port"]),
        upstream=args.upstream or config["upstream"]["url"],
        default_session=config["defaults"]["session_id"],
        default_branch=config["defaults"]["branch_id"],
        max_file_bytes=(
            int(float(config["storage"]["max_mb"]) * 1024 * 1024)
            if config["storage"].get("max_mb")
            else None
        ),
        reranker_listen_host=str(reranker_listen.get("host", "127.0.0.1"))
        if reranker_enabled else None,
        reranker_listen_port=int(reranker_listen.get("port", 8083))
        if reranker_enabled else None,
        reranker_llama_cpp_host=str(
            reranker_llama_cpp.get("host", "127.0.0.1")
        )
        if reranker_enabled else None,
        reranker_llama_cpp_port=int(reranker_llama_cpp.get("port", 8082))
        if reranker_enabled else None,
    )


if __name__ == "__main__":
    main()
