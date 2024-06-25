// Copyright (C) 2020-2024 Stacks Open Internet Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.

use std::env;
use std::sync::atomic::Ordering;
use std::time::Duration;

use libsigner::v0::messages::{
    BlockRejection, BlockResponse, MessageSlotID, RejectCode, SignerMessage,
};
use libsigner::{BlockProposal, SignerSession, StackerDBSession};
use stacks::chainstate::nakamoto::{NakamotoBlock, NakamotoBlockHeader};
use stacks::chainstate::stacks::boot::MINERS_NAME;
use stacks::codec::StacksMessageCodec;
use stacks::libstackerdb::StackerDBChunkData;
use stacks::types::chainstate::StacksPrivateKey;
use stacks::types::PublicKey;
use stacks::util::secp256k1::Secp256k1PublicKey;
use stacks::util_lib::boot::boot_code_id;
use stacks_signer::client::{SignerSlotID, StackerDB};
use stacks_signer::runloop::State;
use stacks_signer::v0::SpawnedSigner;
use tracing_subscriber::prelude::*;
use tracing_subscriber::{fmt, EnvFilter};

use super::SignerTest;
use crate::tests::nakamoto_integrations::{boot_to_epoch_3_reward_set, next_block_and};
use crate::tests::neon_integrations::next_block_and_wait;
use crate::BurnchainController;

impl SignerTest<SpawnedSigner> {
    /// Run the test until the epoch 3 boundary
    fn boot_to_epoch_3(&mut self) {
        boot_to_epoch_3_reward_set(
            &self.running_nodes.conf,
            &self.running_nodes.blocks_processed,
            &self.signer_stacks_private_keys,
            &self.signer_stacks_private_keys,
            &mut self.running_nodes.btc_regtest_controller,
        );
        debug!("Waiting for signer set calculation.");
        let mut reward_set_calculated = false;
        let short_timeout = Duration::from_secs(30);
        let now = std::time::Instant::now();
        // Make sure the signer set is calculated before continuing or signers may not
        // recognize that they are registered signers in the subsequent burn block event
        let reward_cycle = self.get_current_reward_cycle() + 1;
        while !reward_set_calculated {
            let reward_set = self
                .stacks_client
                .get_reward_set_signers(reward_cycle)
                .expect("Failed to check if reward set is calculated");
            reward_set_calculated = reward_set.is_some();
            if reward_set_calculated {
                debug!("Signer set: {:?}", reward_set.unwrap());
            }
            std::thread::sleep(Duration::from_secs(1));
            assert!(
                now.elapsed() < short_timeout,
                "Timed out waiting for reward set calculation"
            );
        }
        debug!("Signer set calculated");

        // Manually consume one more block to ensure signers refresh their state
        debug!("Waiting for signers to initialize.");
        next_block_and_wait(
            &mut self.running_nodes.btc_regtest_controller,
            &self.running_nodes.blocks_processed,
        );
        let now = std::time::Instant::now();
        loop {
            self.send_status_request();
            let states = self.wait_for_states(short_timeout);
            if states
                .iter()
                .all(|state| state == &State::RegisteredSigners)
            {
                break;
            }
            assert!(
                now.elapsed() < short_timeout,
                "Timed out waiting for signers to be registered"
            );
            std::thread::sleep(Duration::from_secs(1));
        }
        debug!("Singers initialized");

        self.run_until_epoch_3_boundary();

        let commits_submitted = self.running_nodes.commits_submitted.clone();

        info!("Waiting 1 burnchain block for miner VRF key confirmation");
        // Wait one block to confirm the VRF register, wait until a block commit is submitted
        next_block_and(&mut self.running_nodes.btc_regtest_controller, 60, || {
            let commits_count = commits_submitted.load(Ordering::SeqCst);
            Ok(commits_count >= 1)
        })
        .unwrap();
        info!("Ready to mine Nakamoto blocks!");
    }

    // Only call after already past the epoch 3.0 boundary
    fn mine_and_verify_confirmed_naka_block(&mut self, timeout: Duration, num_signers: usize) {
        info!("------------------------- Try mining one block -------------------------");
        self.mine_nakamoto_block(timeout);

        // Verify that the signers accepted the proposed block, sending back a validate ok response
        let proposed_signer_signature_hash = self.wait_for_validate_ok_response(timeout);
        let message = proposed_signer_signature_hash.0;

        info!("------------------------- Test Block Signed -------------------------");
        // Verify that the signers signed the proposed block
        let signature = self.wait_for_confirmed_block_v0(&proposed_signer_signature_hash, timeout);

        info!("Got {} signatures", signature.len());

        assert_eq!(signature.len(), num_signers);

        let reward_cycle = self.get_current_reward_cycle();
        let signers = self.get_reward_set_signers(reward_cycle);

        // Verify that the signers signed the proposed block
        let all_signed = signers.iter().zip(signature).all(|(signer, signature)| {
            let stacks_public_key = Secp256k1PublicKey::from_slice(signer.signing_key.as_slice())
                .expect("Failed to convert signing key to StacksPublicKey");

            // let valid = stacks_public_key.verify(message, signature);
            let valid = stacks_public_key
                .verify(&message, &signature)
                .expect("Failed to verify signature");
            if !valid {
                error!(
                    "Failed to verify signature for signer: {:?}",
                    stacks_public_key
                );
            }
            valid
        });
        assert!(all_signed);
    }

    // Only call after already past the epoch 3.0 boundary
    fn run_until_burnchain_height_nakamoto(
        &mut self,
        timeout: Duration,
        burnchain_height: u64,
        num_signers: usize,
    ) {
        let current_block_height = self
            .running_nodes
            .btc_regtest_controller
            .get_headers_height();
        let total_nmb_blocks_to_mine = burnchain_height.saturating_sub(current_block_height);
        debug!("Mining {total_nmb_blocks_to_mine} Nakamoto block(s) to reach burnchain height {burnchain_height}");
        for _ in 0..total_nmb_blocks_to_mine {
            self.mine_and_verify_confirmed_naka_block(timeout, num_signers);
        }
    }
}

#[test]
#[ignore]
/// Test that a signer can respond to an invalid block proposal
///
/// Test Setup:
/// The test spins up five stacks signers, one miner Nakamoto node, and a corresponding bitcoind.
///
/// Test Execution:
/// The stacks node is advanced to epoch 3.0 reward set calculation to ensure the signer set is determined.
/// An invalid block proposal is forcibly written to the miner's slot to simulate the miner proposing a block.
/// The signers process the invalid block by first verifying it against the stacks node block proposal endpoint.
/// The signers then broadcast a rejection of the miner's proposed block back to the respective .signers-XXX-YYY contract.
///
/// Test Assertion:
/// Each signer successfully rejects the invalid block proposal.
fn block_proposal_rejection() {
    if env::var("BITCOIND_TEST") != Ok("1".into()) {
        return;
    }

    tracing_subscriber::registry()
        .with(fmt::layer())
        .with(EnvFilter::from_default_env())
        .init();

    info!("------------------------- Test Setup -------------------------");
    let num_signers = 5;
    let mut signer_test: SignerTest<SpawnedSigner> = SignerTest::new(num_signers);
    signer_test.boot_to_epoch_3();
    let short_timeout = Duration::from_secs(30);

    info!("------------------------- Send Block Proposal To Signers -------------------------");
    let miners_contract_id = boot_code_id(MINERS_NAME, false);
    let mut session = StackerDBSession::new(
        &signer_test.running_nodes.conf.node.rpc_bind,
        miners_contract_id.clone(),
    );
    let block = NakamotoBlock {
        header: NakamotoBlockHeader::empty(),
        txs: vec![],
    };
    let block_signer_signature_hash = block.header.signer_signature_hash();
    let burn_height = signer_test
        .running_nodes
        .btc_regtest_controller
        .get_headers_height();
    let reward_cycle = signer_test.get_current_reward_cycle();
    let message = SignerMessage::BlockProposal(BlockProposal {
        block,
        burn_height,
        reward_cycle,
    });
    let miner_sk = signer_test
        .running_nodes
        .conf
        .miner
        .mining_key
        .expect("No mining key");

    // Submit the block proposal to the miner's slot
    let mut chunk = StackerDBChunkData::new(0, 1, message.serialize_to_vec());
    chunk.sign(&miner_sk).expect("Failed to sign message chunk");
    debug!("Produced a signature: {:?}", chunk.sig);
    let result = session.put_chunk(&chunk).expect("Failed to put chunk");
    debug!("Test Put Chunk ACK: {result:?}");
    assert!(
        result.accepted,
        "Failed to submit block proposal to signers"
    );

    info!("------------------------- Test Block Proposal Rejected -------------------------");
    // Verify that the node correctly rejected the node
    let proposed_signer_signature_hash =
        signer_test.wait_for_validate_reject_response(short_timeout);
    assert_eq!(proposed_signer_signature_hash, block_signer_signature_hash);

    let mut stackerdb = StackerDB::new(
        &signer_test.running_nodes.conf.node.rpc_bind,
        StacksPrivateKey::new(), // We are just reading so don't care what the key is
        false,
        reward_cycle,
        SignerSlotID(0), // We are just reading so again, don't care about index.
    );

    let signer_slot_ids: Vec<_> = signer_test
        .get_signer_indices(reward_cycle)
        .iter()
        .map(|id| id.0)
        .collect();
    assert_eq!(signer_slot_ids.len(), num_signers);

    let messages: Vec<SignerMessage> = StackerDB::get_messages(
        stackerdb
            .get_session_mut(&MessageSlotID::BlockResponse)
            .expect("Failed to get BlockResponse stackerdb session"),
        &signer_slot_ids,
    )
    .expect("Failed to get message from stackerdb");
    for message in messages {
        if let SignerMessage::BlockResponse(BlockResponse::Rejected(BlockRejection {
            reason: _reason,
            reason_code,
            signer_signature_hash,
        })) = message
        {
            assert_eq!(signer_signature_hash, block_signer_signature_hash);
            assert!(matches!(reason_code, RejectCode::ValidationFailed(_)));
        } else {
            panic!("Unexpected message type");
        }
    }
    signer_test.shutdown();
}

// Basic test to ensure that miners are able to gather block responses
// from signers and create blocks.
#[test]
#[ignore]
fn miner_gather_signatures() {
    if env::var("BITCOIND_TEST") != Ok("1".into()) {
        return;
    }

    tracing_subscriber::registry()
        .with(fmt::layer())
        .with(EnvFilter::from_default_env())
        .init();

    info!("------------------------- Test Setup -------------------------");
    let num_signers = 5;
    let mut signer_test: SignerTest<SpawnedSigner> = SignerTest::new(num_signers);
    signer_test.boot_to_epoch_3();
    let timeout = Duration::from_secs(30);

    info!("------------------------- Test Mine and Verify Confirmed Nakamoto Block -------------------------");
    signer_test.mine_and_verify_confirmed_naka_block(timeout, num_signers);

    // Test prometheus metrics response
    #[cfg(feature = "monitoring_prom")]
    {
        let metrics_response = signer_test.get_signer_metrics();

        // Because 5 signers are running in the same process, the prometheus metrics
        // are incremented once for every signer. This is why we expect the metric to be
        // `5`, even though there is only one block proposed.
        let expected_result = format!("stacks_signer_block_proposals_received {}", num_signers);
        assert!(metrics_response.contains(&expected_result));
        let expected_result = format!(
            "stacks_signer_block_responses_sent{{response_type=\"accepted\"}} {}",
            num_signers
        );
        assert!(metrics_response.contains(&expected_result));
    }
}

#[test]
#[ignore]
/// Test that signers can handle a transition between Nakamoto reward cycles
///
/// Test Setup:
/// The test spins up five stacks signers, one miner Nakamoto node, and a corresponding bitcoind.
/// The stacks node is then advanced to Epoch 3.0 boundary to allow block signing.
///
/// Test Execution:
/// The node mines 2 full Nakamoto reward cycles, sending blocks to observing signers to sign and return.
///
/// Test Assertion:
/// All signers sign all blocks successfully.
/// The chain advances 2 full reward cycles.
fn mine_2_nakamoto_reward_cycles() {
    if env::var("BITCOIND_TEST") != Ok("1".into()) {
        return;
    }

    tracing_subscriber::registry()
        .with(fmt::layer())
        .with(EnvFilter::from_default_env())
        .init();

    info!("------------------------- Test Setup -------------------------");
    let nmb_reward_cycles = 2;
    let num_signers = 5;
    let mut signer_test: SignerTest<SpawnedSigner> = SignerTest::new(num_signers);
    let timeout = Duration::from_secs(200);
    signer_test.boot_to_epoch_3();
    let curr_reward_cycle = signer_test.get_current_reward_cycle();
    // Mine 2 full Nakamoto reward cycles (epoch 3 starts in the middle of one, hence the + 1)
    let next_reward_cycle = curr_reward_cycle.saturating_add(1);
    let final_reward_cycle = next_reward_cycle.saturating_add(nmb_reward_cycles);
    let final_reward_cycle_height_boundary = signer_test
        .running_nodes
        .btc_regtest_controller
        .get_burnchain()
        .reward_cycle_to_block_height(final_reward_cycle)
        .saturating_sub(1);

    info!("------------------------- Test Mine 2 Nakamoto Reward Cycles -------------------------");
    signer_test.run_until_burnchain_height_nakamoto(
        timeout,
        final_reward_cycle_height_boundary,
        num_signers,
    );

    let current_burnchain_height = signer_test
        .running_nodes
        .btc_regtest_controller
        .get_headers_height();
    assert_eq!(current_burnchain_height, final_reward_cycle_height_boundary);
    signer_test.shutdown();
}
