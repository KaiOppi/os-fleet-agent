<?php

namespace OPNsense\Fleet;

use OPNsense\Base\IndexController as BaseIndexController;

class IndexController extends BaseIndexController
{
    public function indexAction()
    {
        $this->view->generalForm = $this->getForm("general");
        $this->view->pick('OPNsense/Fleet/index');
    }
}
