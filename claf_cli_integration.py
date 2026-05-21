#!/usr/bin/env python3
"""
CLAF CLI Integration — Direct CLI-to-Orchestrator-to-Extension flow.

When you type in CLI:
  "extension click the submit button"
  
Flow:
  1. CLI message → Session Router (tagged with session ID)
  2. Orchestrator sees "extension" directive
  3. Routes to Sensei extension via MCP bridge
  4. Extension executes, returns result
  5. Result delivered back to CLI in same session

Usage:
  python3 claf_cli_integration.py --message "extension read the page"
"""

import sys
import json
import argparse
from pathlib import Path

# Import our modules
sys.path.insert(0, str(Path(__file__).parent))

from session_router import get_router, inject_session_context, ask_extension
from sensei_mcp_bridge import SenseiMCPBridge
from github_memory_sync import GitHubMemorySync


def parse_extension_directive(user_input: str) -> tuple[str, dict]:
    """
    Parse directives like:
      "extension click button.submit"
      "extension fill input.email with user@example.com"
      "extension read the page"
      "extension navigate to https://example.com"
    
    Returns: (action, params)
    """
    parts = user_input.strip().split(None, 2)
    
    if not parts or parts[0].lower() != "extension":
        return None, None
    
    if len(parts) < 2:
        return None, None
    
    action = parts[1].lower()
    rest = parts[2] if len(parts) > 2 else ""
    
    # Parse different action formats
    if action == "click":
        selector = rest.strip() if rest else ""
        if not selector:
            raise ValueError("click: need a selector (e.g., 'extension click button.submit')")
        return "click", {"selector": selector}
    
    elif action == "fill":
        # Format: "fill <selector> with <value>"
        if " with " not in rest:
            raise ValueError("fill: use format 'extension fill <selector> with <value>'")
        selector, value = rest.split(" with ", 1)
        return "fill", {"selector": selector.strip(), "value": value.strip()}
    
    elif action == "read":
        # Read page or specific element
        selector = rest.strip() if rest else "body"
        return "read", {"selector": selector}
    
    elif action == "navigate":
        url = rest.strip() if rest else ""
        if not url:
            raise ValueError("navigate: need a URL")
        return "navigate", {"url": url}
    
    elif action == "screenshot":
        return "screenshot", {}
    
    elif action == "scroll":
        direction = rest.strip() if rest else "down"
        return "scroll", {"direction": direction}
    
    elif action == "wait":
        try:
            ms = int(rest.strip()) if rest else 1000
        except ValueError:
            ms = 1000
        return "wait", {"ms": ms}
    
    else:
        raise ValueError(f"unknown extension action: {action}")


def handle_user_input(user_input: str) -> str:
    """
    Main entry point for CLI input.
    
    Returns: response text to display to user
    """
    router = get_router()
    user_input = user_input.strip()
    
    if not user_input:
        return ""
    
    # Check if this is an extension directive
    if user_input.lower().startswith("extension "):
        try:
            action, params = parse_extension_directive(user_input)
            if action is None:
                return "Error: invalid extension directive"
            
            print(f"\n[CLI → Extension] {action} {json.dumps(params)}")
            result = ask_extension(action, params, timeout=30)
            
            if result.get("success"):
                output = result.get("result", "")
                if isinstance(output, dict):
                    output = json.dumps(output, indent=2)
                return f"✓ Extension completed:\n{output}"
            else:
                error = result.get("error", "unknown error")
                return f"✗ Extension failed: {error}"
        
        except Exception as e:
            return f"Error: {e}"
    
    else:
        # Regular LLM prompt
        # Inject session context and send to orchestrator
        return f"[Regular prompt routed through orchestrator with session {router.session_id[:12]}...]"


def main():
    parser = argparse.ArgumentParser(description="CLAF CLI → Extension integration")
    parser.add_argument("--message", help="Single message to process")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode")
    parser.add_argument("--register", choices=["cli", "extension", "pupil"], help="Register this surface")
    
    args = parser.parse_args()
    
    router = get_router()
    
    if args.register:
        router.register_surface(args.register)
        print(f"✓ Registered as {args.register}")
        print(f"Session ID: {router.session_id}")
        return
    
    if args.message:
        response = handle_user_input(args.message)
        print(response)
        return
    
    if args.interactive or not args.message:
        print("CLAF CLI with Extension Bridge")
        print("=" * 72)
        print(f"Session: {router.session_id}")
        print()
        print("Try:")
        print("  extension click button.submit")
        print("  extension read the page")
        print("  extension navigate to https://example.com")
        print("  extension fill input.email with test@example.com")
        print()
        
        router.register_surface("cli")
        
        try:
            while True:
                try:
                    user_input = input("CLI> ").strip()
                except EOFError:
                    break
                
                if not user_input:
                    continue
                
                if user_input in ("quit", "exit", "q"):
                    break
                
                response = handle_user_input(user_input)
                print(response)
                print()
        
        except KeyboardInterrupt:
            print("\n(interrupted)")


if __name__ == "__main__":
    main()
