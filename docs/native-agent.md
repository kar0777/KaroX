# Native agent

`karox` accepts natural-language tasks after an API model is connected.
`karox agent run` is the non-interactive form. Both use `AgentKernel` and the
same repository-bound `CoreRuntime`, capability policy, session lease, audit,
verification and recovery used by bridges. The provider never receives direct
filesystem or unrestricted process access.

