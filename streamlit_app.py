import traceback

import streamlit as st


try:
    from app_scenario import main

    main()
except Exception as exc:
    st.error("アプリの起動に失敗しました。")
    st.exception(exc)
    traceback.print_exc()
