## How this evaluation works

$scripted
The project folder the human chose is $project. Run every shell command through the run wrapper, with the whole command as one quoted argument, for example:

    $run 'vecten next --json'

The wrapper runs the command in the project folder, with the environment the evaluation needs (the vecten CLI, the mocked cloud and GitHub, the certificate bundle), and logs it. It gives the command no terminal and no standard input. Some commands start Docker containers, reach https hosts on 127.0.0.1, or take several minutes. If the sandbox blocks one (Docker socket, network, "Operation not permitted"), run it again with the sandbox disabled. Give long commands a Bash timeout of up to 600000 ms. Create a branch with `git checkout -b <name>`. Reading files in the project folder with your file tools is fine. Do not read anything else in $checkout except the skill path and the input files named above.

When you need the human to do something you cannot do from your shell, tell them through the human helper and wait for their reply. That covers typing a secret at a hidden prompt in their own terminal, opening a page in their browser, having someone run something, or anything else that needs them. Call it with a Bash timeout of 600000 ms:

    $human '<your message to the human>'

It prints their reply. If it prints "no reply yet", run the `--wait <n>` command it names. $answered
$mcp_client
Keep transcript.md in your outputs folder as you go, in chronological order. Record:
- each message you would show the human, verbatim;
- each question you put to the human, verbatim, followed by the reply you took;
- each command you ran, with its exit code and the part of its output you relied on.

Never write a secret value into it. When the conversation ends, finish transcript.md and reply with a short summary of where things stand.
