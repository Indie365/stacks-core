import fc from "fast-check";

import { Simnet } from "@hirosystems/clarinet-sdk";
import { StacksPrivateKey } from "@stacks/transactions";
import { StackingClient } from "@stacks/stacking";

export type StxAddress = string;

export type Stub = {
  stackingMinimum: number;
  wallets: Map<StxAddress, Wallet>;
};

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
  ustxBalance: number;
  isStacking: boolean;
  hasDelegated: boolean;
  hasPoolMembers: StxAddress[];
  delegatedTo: StxAddress;
  delegatedMaxAmount: number;
  delegatedUntilBurnHt: number;
  amountLocked: number;
  amountUnlocked: number;
  unlockHeight: number;
  allowedContractCaller: StxAddress;
  callerAllowedBy: StxAddress[];
};

export type PoxCommand = fc.Command<Stub, Real>;