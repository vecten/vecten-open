# Design

Before rendering, inspect only the target's top-level entries. Allow the recorded answers file,
the Claude Code directory and Finder's metadata file. The scaffold writes from its own template.
Count the files it writes so reporting never needs to walk the session directory.

Use the existing scaffold name pattern for intake answers and direct initialization. Show the
same format in the guide question and in validation errors. Validate before saving the answer.
