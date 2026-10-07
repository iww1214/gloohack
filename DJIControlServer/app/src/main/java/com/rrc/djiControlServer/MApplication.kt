package com.rrc.djiControlServer


import android.app.Application
import android.content.Context
import com.cySdkyc.clx.Helper

class MApplication: Application() {

    override fun attachBaseContext(base: Context?) {
        super.attachBaseContext(base)
        Helper.install(this)
    }

}