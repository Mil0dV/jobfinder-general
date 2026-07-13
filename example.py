"""Example script showing how to use the Climate Job Finder Agent programmatically."""

from agent import root_agent


def main():
    """Run example queries with the climate job finder agent."""

    print("=" * 60)
    print("Climate & Green Tech Job Finder - Example Usage")
    print("=" * 60)
    print()

    # Example 1: Software engineer in climate tech
    print("Example 1: Searching for software engineer in climate tech...")
    print("-" * 60)
    response1 = root_agent.run("Find me software engineer jobs in climate tech")
    print(response1)
    print()

    # Example 2: Remote renewable energy positions
    print("Example 2: Searching for remote renewable energy positions...")
    print("-" * 60)
    response2 = root_agent.run("Show me remote renewable energy engineer positions")
    print(response2)
    print()

    # Example 3: Entry-level sustainability jobs
    print("Example 3: Searching for entry-level sustainability jobs...")
    print("-" * 60)
    response3 = root_agent.run("I'm looking for entry-level sustainability analyst jobs")
    print(response3)
    print()

    # Example 4: Climate data scientist
    print("Example 4: Searching for climate data scientist...")
    print("-" * 60)
    response4 = root_agent.run("Find climate data scientist jobs in San Francisco")
    print(response4)
    print()


if __name__ == "__main__":
    main()
