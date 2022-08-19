[![GPLv3 License](https://img.shields.io/badge/License-GPL%20v3-yellow.svg)](https://opensource.org/licenses/)
# Stacks 2.0

Reference implementation of the [Stacks blockchain](https://github.com/stacks-network/stacks) in Rust.

Stacks 2.0 is a layer-1 blockchain that connects to Bitcoin for security and enables decentralized apps and predictable smart contracts. Stacks 2.0 implements [Proof of Transfer (PoX)](https://community.stacks.org/pox) mining that anchors to Bitcoin security. Leader election happens at the Bitcoin blockchain and Stacks (STX) miners write new blocks on the separate Stacks blockchain. With PoX there is no need to modify Bitcoin to enable smart contracts and apps around it. See [this page](https://github.com/stacks-network/stacks) for more details and resources.

### Platform support

Officially supported platforms: `Linux 64-bit`, `MacOS 64-bit`, `Windows 64-bit`.

Platforms with second-tier status _(builds are provided but not tested)_: `MacOS Apple Silicon (ARM64)`, `Linux ARMv7`, `Linux ARM64`.

For help cross-compiling on memory-constrained devices, please see the community supported documentation here: [Cross Compiling](https://github.com/dantrevino/cross-compiling-stacks-blockchain/blob/master/README.md).


## Getting started

[Here](http://docs.stacks.co/docs/blockchain/) is a full guide on how to get started by downloading the Stacks blockchain and building it locally.

We also have guides to setup your own [Stacks node or miners](https://docs.stacks.co/docs/nodes-and-miners/).

## Contributing

For more information on how to contribute to this repository please refer to [CONTRIBUTORS](CONTRIBUTORS.md).

For more information on Stacks Improvement Proposals (SIPs) please refer to [SIPs documentation](https://docs.stacks.co/docs/governance/sips).

[Here](https://docs.stacks.co/docs/contribute/) you can find other ways to contribute to the Stacks ecosystem. 

## Community and Further reading

For more information please refer to the [Stacks official documentation](https://docs.stacks.co/).

Technical papers:

- ["PoX: Proof of Transfer Mining with Bitcoin"](https://community.stacks.org/pox), May 2020
- ["Stacks 2.0: Apps and Smart Contracts for Bitcoin"](https://stacks.org/stacks), Dec 2020

You can get in contact with the Stacks Community on:

* [Forum](https://forum.stacks.org)
* [Discord](https://discord.com/invite/XYdRyhf)
* [Mailing list](https://newsletter.stacks.org/)
* [Meetups](https://www.meetup.com/topics/blockstack/)
* [Stacks YouTube channel](https://www.youtube.com/channel/UC3J2iHnyt2JtOvtGVf_jpHQ)
* [Stacks Community Youtube channel](https://www.youtube.com/channel/UCp7D42MyHXk4-J2TtF5I0Kg)