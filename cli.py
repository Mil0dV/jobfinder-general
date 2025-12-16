#!/usr/bin/env python3
"""CLI interface for the Climate Job Finder Agent."""

import argparse
import sys
import os
from agent import root_agent


def main():
    """Run the climate job finder with CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Find climate and green tech job opportunities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "Software Engineer"
  %(prog)s "Data Scientist" --location "San Francisco"
  %(prog)s "Renewable Energy Engineer" --job-type remote
  %(prog)s "Climate Policy Analyst" --experience senior --location "Washington DC"
        """
    )
    
    parser.add_argument(
        "job_title",
        help="Job title or role to search for (e.g., 'Software Engineer', 'Data Scientist')"
    )
    
    parser.add_argument(
        "-l", "--location",
        default="",
        help="Location or city for the job search (default: any location)"
    )
    
    parser.add_argument(
        "-t", "--job-type",
        default="",
        choices=["", "full-time", "part-time", "contract", "remote"],
        help="Type of employment (default: any type)"
    )
    
    parser.add_argument(
        "-e", "--experience",
        default="",
        choices=["", "entry", "mid", "senior"],
        help="Experience level (default: any level)"
    )
    
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Start interactive mode after initial search"
    )
    
    args = parser.parse_args()
    
    # Build the query
    query_parts = [f"Find {args.job_title} jobs in the climate and green tech sector"]
    
    if args.location:
        query_parts.append(f"in {args.location}")
    if args.job_type:
        query_parts.append(f"({args.job_type})")
    if args.experience:
        query_parts.append(f"for {args.experience}-level candidates")
    
    query = " ".join(query_parts)
    
    print("=" * 70)
    print("🌱 Climate & Green Tech Job Finder")
    print("=" * 70)
    # print(os.getenv("OPENAI_API_KEY"))
    print(f"\nSearching for: {args.job_title}")
    if args.location:
        print(f"Location: {args.location}")
    if args.job_type:
        print(f"Job Type: {args.job_type}")
    if args.experience:
        print(f"Experience: {args.experience}")
    print("\n" + "-" * 70 + "\n")
    
    # Run the agent
    try:
        response = root_agent.run(query)
        print(response)
        
        if args.interactive:
            print("\n" + "=" * 70)
            print("Entering interactive mode. Type 'exit' or 'quit' to end.")
            print("=" * 70 + "\n")
            
            while True:
                try:
                    user_input = input("\n🌱 You: ").strip()
                    if user_input.lower() in ['exit', 'quit', 'q']:
                        print("\nGoodbye! Good luck with your climate tech job search! 🌍")
                        break
                    
                    if not user_input:
                        continue
                    
                    response = root_agent.run(user_input)
                    print(f"\n🤖 Agent: {response}")
                    
                except KeyboardInterrupt:
                    print("\n\nGoodbye! Good luck with your climate tech job search! 🌍")
                    break
                except EOFError:
                    break
    
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        print("\nMake sure you have set up your .env file with OPENAI_API_KEY", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
