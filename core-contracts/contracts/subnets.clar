;; The .subnets contract

(define-constant CONTRACT_ADDRESS (as-contract tx-sender))

;; Error codes
(define-constant ERR_BLOCK_ALREADY_COMMITTED 1)
(define-constant ERR_INVALID_MINER 2)
(define-constant ERR_VALIDATION_FAILED 3)
(define-constant ERR_CONTRACT_CALL_FAILED 4)
(define-constant ERR_TRANSFER_FAILED 5)
(define-constant ERR_MAP_INSERT_FAILED 6)
(define-constant ERR_TRANSFER_DOES_NOT_EXIST 7)
(define-constant ERR_ALREADY_ACKED 8)

;; Map from Stacks block height to block commit
(define-map block-commits uint (buff 32))

;; List of miners
(define-constant miners (list 'SPAXYA5XS51713FDTQ8H94EJ4V579CXMTRNBZKSF 'SP3X6QWWETNBZWGBK6DRGTR1KX50S74D3433WDGJY
    'ST1AW6EKPGT61SQ9FNVDS17RKNWT8ZP582VF9HSCP 'ST1SJ3DTE5DN7X54YDH5D64R3BCB6A2AG2ZQ8YPD5))

;; Testing info for 'ST1SJ3DTE5DN7X54YDH5D64R3BCB6A2AG2ZQ8YPD5:
;;      secret_key: 7287ba251d44a4d3fd9276c88ce34c5c52a038955511cccaf77e61068649c17801
;;      btc_address: mr1iPkD9N3RJZZxXRk7xF9d36gffa6exNC

;; Use trait declarations
(use-trait nft-trait .nft-trait-standard.nft-trait)
(use-trait ft-trait .ft-trait-standard.ft-trait)


;; Helper function for fold: if a == b, return none; else return b
(define-private (is-principal-eq (miner-a principal) (search-for (optional principal)))
    (if (is-eq (some miner-a) search-for)
        none
        search-for
    )
)
;; Helper function: returns a boolean indicating whether the given principal is in the list of miners
(define-private (is-miner (miner principal))
   (let ((fold-result (fold is-principal-eq miners (some miner))))
        (is-none fold-result)
   ))

;; Helper function: determines whether the commit-block operation can be carried out
(define-private (can-commit-block? (commit-block-height uint))
    (begin
        ;; check no block has been committed at this height
        (asserts! (is-none (map-get? block-commits commit-block-height)) (err ERR_BLOCK_ALREADY_COMMITTED))

        ;; check that the tx sender is one of the miners
        (asserts! (is-miner tx-sender) (err ERR_INVALID_MINER))

        (ok true)
    )
)

;; Helper function: modifies the block-commits map with a new commit and prints related info
(define-private (inner-commit-block (block (buff 32)) (commit-block-height uint))
    (begin
        (map-set block-commits commit-block-height block)
        (print { event: "block-commit", block-commit: block})
        (ok block)
    )
)

;; Subnets miners call this to commit a block at a particular height
(define-public (commit-block (block (buff 32)) (commit-block-height uint))
    (begin
        (unwrap! (can-commit-block? commit-block-height) (err ERR_VALIDATION_FAILED))
        (inner-commit-block block commit-block-height)
    )
)


;; Implement functions below in M2

;; FOR NFTs


;; user: deposit asset
(define-public (deposit-nft-asset (id uint) (sender principal) (nft-contract <nft-trait>))
    (let (
        (call-result (contract-call? nft-contract transfer id sender CONTRACT_ADDRESS))
        (transfer-result (unwrap! call-result (err ERR_CONTRACT_CALL_FAILED)))
    )
        (asserts! transfer-result (err ERR_TRANSFER_FAILED))

        (print { event: "deposit-nft", nft-id: id })

        (ok true)
    )
)

(define-public (inner-withdraw-nft-asset (id uint) (recipient principal) (nft-contract <nft-trait>))
    (let (
        (call-result (as-contract (contract-call? nft-contract transfer id CONTRACT_ADDRESS recipient)))
        (transfer-result (unwrap! call-result (err ERR_CONTRACT_CALL_FAILED)))
    )
        (asserts! transfer-result (err ERR_TRANSFER_FAILED))

        (print { event: "deposit-nft", nft-id: id })

        (ok true)
    )
)

;; user: issue withdraw request
(define-public (withdraw-nft-asset (id uint) (recipient principal) (nft-contract <nft-trait>))
    (begin
        (asserts! (is-miner tx-sender) (err ERR_INVALID_MINER))
        (inner-withdraw-nft-asset id recipient nft-contract)
    )
)
