from agent.agent_pool import AgentPool
from bootstrap import build_runtime, load_app
from core.text_resolver import LocalTextResolver
from perception.ui_detector import UIDetector
from user_interface.cli_handler import run_cli


def main() -> None:
    config, logger = load_app("config.yaml")
    runtime = build_runtime(config, logger)

    for address in config.get("adb", {}).get("wireless", []) or []:
        result = runtime.device_manager.connect(address)
        logger.info("adb connect %s: %s", result["address"], result["message"])
    devices = runtime.device_manager.discover_devices()

    # CLI-only: free-text instructions ("点设置") are resolved to coordinates on
    # screen, and one agent per device serves them.
    ui_detector = UIDetector(
        logger, runtime.capturer, dump_matcher=runtime.dump_matcher, ocr_engine=runtime.ocr
    )
    text_resolver = LocalTextResolver(ui_detector, logger)

    agent_pool = AgentPool(logger, text_resolver, config, screenshot_capturer=runtime.capturer)
    agent_pool.sync_from_devices(devices)

    logger.info("Initialized %d agent(s).", len(agent_pool.list_agents()))
    run_cli(agent_pool, runtime.device_manager, logger, config=config, task_engine=runtime.engine)


if __name__ == "__main__":
    main()
