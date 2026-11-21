def migrate(cr, version):
    # "Divalidasi Oleh" is recorded from now on; for the stock moves done before, use their last editor.
    cr.execute("""
        UPDATE stock_move
           SET mina_done_by_id = write_uid
         WHERE state = 'done' AND mina_done_by_id IS NULL
    """)
