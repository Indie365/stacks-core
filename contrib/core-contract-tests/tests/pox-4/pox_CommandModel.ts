import fc from "fast-check";

import { Simnet } from "@hirosystems/clarinet-sdk";
import {
  ClarityValue,
  cvToValue,
  StacksPrivateKey,
} from "@stacks/transactions";
import { StackingClient } from "@stacks/stacking";

export type StxAddress = string;
export type BtcAddress = string;
export type CommandTag = string;

export class Stub {
  readonly wallets: Map<StxAddress, Wallet>;
  readonly statistics: Map<string, number>;
  readonly stackers: Map<StxAddress, Stacker>;
  stackingMinimum: number;
  nextRewardSetIndex: number;
  lastRefreshedCycle: number;

  constructor(
    wallets: Map<StxAddress, Wallet>,
    stackers: Map<StxAddress, Stacker>,
    statistics: Map<CommandTag, number>,
  ) {
    this.wallets = wallets;
    this.statistics = statistics;
    this.stackers = stackers;
    this.stackingMinimum = 0;
    this.nextRewardSetIndex = 0;
    this.lastRefreshedCycle = 0;
  }

  trackCommandRun(commandName: string) {
    const count = this.statistics.get(commandName) || 0;
    this.statistics.set(commandName, count + 1);
  }

  reportCommandRuns() {
    console.log("Command run method execution counts:");
    this.statistics.forEach((count, commandName) => {
      console.log(`${commandName}: ${count}`);
    });
  }

  refreshStateForNextRewardCycle(real: Real) {
    const burnBlockHeightResult = real.network.runSnippet("burn-block-height");
    const burnBlockHeight = Number(
      cvToValue(burnBlockHeightResult as ClarityValue),
    );
    const lastRefreshedCycle = this.lastRefreshedCycle;
    const currentRewCycle = Math.floor((Number(burnBlockHeight) - 0) / 1050);

    if (lastRefreshedCycle < currentRewCycle) {
      this.nextRewardSetIndex = 0;

      this.wallets.forEach((w) => {
        const wallet = this.stackers.get(w.stxAddress)!;
        const expiredDelegators = wallet.poolMembers.filter((stackerAddress) =>
          this.stackers.get(stackerAddress)!.delegatedUntilBurnHt <
            burnBlockHeight + 1
        );
        const expiredStackers = wallet.lockedAddresses.filter(
          (stackerAddress) =>
            this.stackers.get(stackerAddress)!.unlockHeight <=
              burnBlockHeight + 1,
        );

        expiredDelegators.forEach((expDelegator) => {
          const expDelegatorIndex = wallet.poolMembers.indexOf(expDelegator);
          wallet.poolMembers.splice(expDelegatorIndex, 1);
        });

        expiredStackers.forEach((expStacker) => {
          const expStackerWallet = this.stackers.get(expStacker)!;
          const expStackerIndex = wallet.lockedAddresses.indexOf(expStacker);
          wallet.lockedAddresses.splice(expStackerIndex, 1);
          wallet.amountToCommit -= expStackerWallet.amountLocked;
        });

        if (
          wallet.unlockHeight > 0 && wallet.unlockHeight <= burnBlockHeight + 1
        ) {
          wallet.isStacking = false;
          wallet.amountUnlocked += wallet.amountLocked;
          wallet.amountLocked = 0;
          wallet.unlockHeight = 0;
          wallet.firstLockedRewardCycle = 0;
        }
        wallet.committedRewCycleIndexes = [];
      });
      this.stackers.forEach((stacker) =>
        process.stdout.write(`${JSON.stringify(stacker)}\n`)
      );

      this.lastRefreshedCycle = currentRewCycle;
    }
  }
}

export type Real = {
  network: Simnet;
};

export type Wallet = {
  label: string;
  stxAddress: string;
  btcAddress: string;
  signerPrvKey: StacksPrivateKey;
  signerPubKey: string;
  stackingClient: StackingClient;
};

export type Stacker = {
  ustxBalance: number;
  isStacking: boolean;
  hasDelegated: boolean;
  lockedAddresses: StxAddress[];
  amountToCommit: number;
  poolMembers: StxAddress[];
  delegatedTo: StxAddress;
  delegatedMaxAmount: number;
  delegatedUntilBurnHt: number;
  delegatedPoxAddress: BtcAddress;
  amountLocked: number;
  amountUnlocked: number;
  unlockHeight: number;
  firstLockedRewardCycle: number;
  allowedContractCaller: StxAddress;
  callerAllowedBy: StxAddress[];
  committedRewCycleIndexes: number[];
};

export type PoxCommand = fc.Command<Stub, Real>;

export const logCommand = (...items: (string | undefined)[]) => {
  // Ensure we only render up to the first 10 items for brevity.
  const renderItems = items.slice(0, 10);
  const columnWidth = 23;
  // Pad each column to the same width.
  const prettyPrint = renderItems.map((content) =>
    content ? content.padEnd(columnWidth) : "".padEnd(columnWidth)
  );
  prettyPrint.push("\n");

  process.stdout.write(prettyPrint.join(""));
};